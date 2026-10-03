import Foundation
import Combine

enum DisplayState: String {
    case blocked = "BLOCKED"
    case running = "RUNNING"
    case passed = "PASS"
    case idle = "IDLE"
    case unknown = "UNKNOWN"
}

extension ObservedTask {
    var displayState: DisplayState {
        let exactResult = finalResult?.executionRef == executionRef ? finalResult : nil
        if ["BLOCKED", "FAILED", "RECOVERY_REQUIRED", "TRANSPORT_UNCERTAIN"].contains(state)
            || retryRequired || exactResult?.status == "BLOCKED" { return .blocked }
        if ["CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING", "FINALIZING",
            "CANCEL_REQUESTED", "CANCELLATION_PENDING"].contains(state)
            || codexRunning { return .running }
        if ["COMPLETED", "IN_REVIEW"].contains(state), exactResult?.status == "PASS" {
            return .passed
        }
        if ["QUEUED", "STOPPED", "CANCELLED", "COMPLETED", "IN_REVIEW"].contains(state) {
            return .idle
        }
        return .unknown
    }

    var primaryLabel: String { title?.isEmpty == false ? title! : taskRef }

    /// User-facing progress string. The canonical reason for a missing denominator is
    /// presented in a tooltip, never as the primary label.
    var progressLabel: String {
        switch progressPresentation {
        case .determinate(let label, let done, let total): return "\(label) · \(done) / \(total)"
        case .indeterminate(let label, _): return label
        case .unavailable: return "Progress unavailable"
        }
    }

    var progressReason: String { ProgressPresentation.unavailableReason }
}

enum ConnectionState: Equatable {
    case setup, loading, connected, degraded, stale, error(String)
}

enum TimeWindow: String, CaseIterable, Identifiable {
    case hour = "1h", day = "24h", week = "7d"

    var id: String { rawValue }
    var minutes: Int {
        switch self {
        case .hour: return 60
        case .day: return 1440
        case .week: return 10080
        }
    }
}

enum ExecutionOrder: String, CaseIterable, Identifiable {
    case activity = "Latest activity", newest = "Newest first", oldest = "Oldest first"
    var id: String { rawValue }

    func ordered(_ tasks: [ObservedTask]) -> [ObservedTask] {
        tasks.sorted { lhs, rhs in
            let left = self == .activity ? (lhs.lastProgressDate ?? lhs.startedDate) : lhs.startedDate
            let right = self == .activity ? (rhs.lastProgressDate ?? rhs.startedDate) : rhs.startedDate
            if left == right { return lhs.taskRef < rhs.taskRef }
            guard let left else { return false }
            guard let right else { return true }
            return self == .oldest ? left < right : left > right
        }
    }
}

struct TaskGroup: Identifiable {
    let id: String
    let title: String?
    let tasks: [ObservedTask]
}

struct FilterOption: Identifiable, Hashable {
    let name: String
    let count: Int
    var id: String { name }
}

@MainActor
final class MonitorStore: ObservableObject {
    // Canonical pages
    @Published private(set) var active: [ObservedTask] = []
    @Published private(set) var recent: [ObservedTask] = []
    @Published private(set) var selected: ObservedTask?
    @Published private(set) var events: [ObserverEvent] = []
    @Published private(set) var eventCoverage = "UNKNOWN"
    @Published private(set) var eventHasMore = false
    @Published private(set) var activeNextOffset: Int?
    @Published private(set) var recentNextOffset: Int?
    @Published private(set) var health: ObserverHealth?
    @Published private(set) var lastSuccessfulFetch: Date?
    @Published private(set) var lastObservedAt: String?
    @Published private(set) var errorCategory: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var endpointText: String
    @Published private(set) var linkedDeviceID: String?
    @Published private(set) var linkedDeviceName: String?
    var credentialAccount: String { linkedDeviceID.map { "device:\($0)" } ?? "p620-observer" }
    @Published private(set) var syntheticScenario: SyntheticScenario?

    // Presentation state
    @Published var view: MonitorView = .active
    @Published var searchText = ""
    @Published var projectFilter: String?
    @Published var hostFilter: String?
    @Published var timeWindow: TimeWindow = .day
    @Published var executionOrder: ExecutionOrder = .activity
    @Published var settingsPresented = false
    @Published private(set) var selectedRef: String?
    @Published private(set) var selectedExecutionRef: String?
    @Published private(set) var selectedDetailIncomplete = false
    @Published private(set) var localArchives: [LocalArchiveEntry]
    @Published private(set) var navigationHistory: [NavigationEntry] = []
    @Published private(set) var navigationIndex: Int?

    private let defaults: UserDefaults
    private static let archiveKey = "monitor.localArchives.v1"

    var activityService: (any ActivityServing)? { service as? any ActivityServing }
    var activitySource: String { archiveSource }

    private var service: (any ObserverServing)?
    private var pollTask: Task<Void, Never>?
    private var eventCursor: String?
    private var selectionToken = 0
    private var sourceToken = 0
    private var listRequestGeneration = 0
    private var authFailed = false
    private var failureCount = 0
    private var failed = false

    init(service: (any ObserverServing)? = nil, defaults: UserDefaults = .standard) {
        self.defaults = defaults
        localArchives = defaults.data(forKey: Self.archiveKey)
            .flatMap { try? JSONDecoder().decode([LocalArchiveEntry].self, from: $0) } ?? []
        self.service = service
        endpointText = defaults.string(forKey: "monitor.endpoint") ?? ""
        linkedDeviceID = defaults.string(forKey: "monitor.deviceID")
        linkedDeviceName = defaults.string(forKey: "monitor.deviceName")
        if service == nil, !endpointText.isEmpty, let url = URL(string: endpointText) {
            self.service = try? ObserverClient(baseURL: url, credentialAccount: credentialAccount)
        }
    }

    // MARK: - Derived state

    /// Rows are executions, not just task names. A task can have a historical terminal
    /// execution in the recent page while a newer execution is active. Merge exact
    /// identities by observation freshness and keep different executions separate.
    var allTasks: [ObservedTask] { Self.merge(active + recent) }

    private var archiveSource: String {
        syntheticScenario.map { "synthetic:\($0.rawValue)" } ?? endpointText
    }

    var currentArchives: [LocalArchiveEntry] { localArchives.filter { $0.source == archiveSource } }

    func isLocallyArchived(_ task: ObservedTask) -> Bool {
        localArchives.contains { $0.matches(task, source: archiveSource) }
    }

    func archiveLocally(_ task: ObservedTask) {
        guard status(of: task).canArchiveLocally, !isLocallyArchived(task) else { return }
        localArchives.append(LocalArchiveEntry(id: UUID(), source: archiveSource,
                                              taskRef: task.taskRef, executionRef: task.executionRef,
                                              title: task.titleText))
        persistArchives()
        removeHistory(for: task.taskRef)
        if selectedRef == task.taskRef && selectedExecutionRef == task.executionRef {
            selectionToken += 1
            clearSelection()
        }
    }

    func restoreLocalArchive(_ id: UUID) {
        localArchives.removeAll { $0.id == id }
        persistArchives()
    }

    private func persistArchives() {
        guard let data = try? JSONEncoder().encode(localArchives) else { return }
        defaults.set(data, forKey: Self.archiveKey)
    }

    var connection: ConnectionState {
        if let syntheticScenario { return Self.connection(for: syntheticScenario) }
        guard service != nil else { return .setup }
        guard let lastSuccessfulFetch else { return failed ? .error(errorCategory ?? "Connection error") : .loading }
        let interval: TimeInterval = active.isEmpty ? 15 : 2
        if Date().timeIntervalSince(lastSuccessfulFetch) > max(3 * interval, 10) { return .stale }
        if failed { return .error(errorCategory ?? "Connection error") }
        if health?.status != "OK" { return .degraded }
        return .connected
    }

    private static func connection(for scenario: SyntheticScenario) -> ConnectionState {
        switch scenario {
        case .stale: return .stale
        case .offline: return .error("Observer unavailable")
        default: return .connected
        }
    }

    /// Connection segment of the toolbar cluster.
    var connection7: ConnectionState7 {
        if let syntheticScenario { return syntheticScenario.connection }
        switch connection {
        case .connected: return .connected
        case .degraded, .stale: return .degraded
        case .error, .setup, .loading: return .offline
        }
    }

    /// Authority is never inferred from a persisted running flag; it reflects what the
    /// Observer reports, and synthetic scenarios report their own liveness.
    var authority: AuthorityState {
        if let syntheticScenario { return syntheticScenario.authority }
        guard let value = health?.authorityLiveness.uppercased() else { return .unknown }
        switch value {
        case "LIVE", "OK", "HEALTHY": return .live
        case "STALE", "DEGRADED": return .stale
        default: return .unknown
        }
    }

    var freshness: Freshness {
        if let syntheticScenario { return syntheticScenario.freshness }
        switch connection {
        case .connected: return .current
        case .degraded, .stale: return .stale
        case .error, .setup, .loading: return .lastKnown
        }
    }

    var syncText: String {
        if let syntheticScenario { return syntheticScenario.syncText }
        guard let lastSuccessfulFetch else { return "never" }
        return RelativeTime.ago(since: lastSuccessfulFetch)
    }

    var connectivityNote: String? {
        if let syntheticScenario { return syntheticScenario.connectivityNote }
        switch connection {
        case .setup: return "Observer endpoint not configured"
        case .loading: return nil
        case .error(let category): return category
        case .degraded: return "Observer degraded"
        case .stale: return "Snapshot is stale"
        case .connected: return nil
        }
    }

    var connectivityDetail: String? {
        if let syntheticScenario { return syntheticScenario.connectivityDetail }
        switch connection {
        case .setup: return "Open Settings (⌘,) → Devices to pair a nearby host, or use Connection for manual setup."
        case .error: return lastSuccessfulFetch == nil
            ? "No snapshot has been observed yet."
            : "Showing last known data from \(syncText)."
        case .degraded, .stale: return "Showing last known data. Running states are shown as Stale."
        case .loading, .connected: return nil
        }
    }

    var connectivityTail: String? {
        if let syntheticScenario { return syntheticScenario.connectivityTail }
        switch connection {
        case .error: return "retrying with backoff"
        case .degraded, .stale: return "next sync in \(retryDelay)s"
        default: return nil
        }
    }

    var isStaleSnapshot: Bool { freshness != .current }

    func status(of task: ObservedTask) -> MonitorStatus { task.status(freshness: freshness) }

    private var unarchivedTasks: [ObservedTask] { allTasks.filter { !isLocallyArchived($0) } }

    var counts: [MonitorView: Int] {
        var result = Dictionary(uniqueKeysWithValues: MonitorView.allCases.map { ($0, 0) })
        let freshness = freshness
        for task in filteredTasks {
            let status = task.status(freshness: freshness)
            for view in MonitorView.allCases where view.matches(status) {
                result[view, default: 0] += 1
            }
        }
        return result
    }

    var hostOptions: [FilterOption] {
        Dictionary(grouping: unarchivedTasks, by: { $0.hostText })
            .map { FilterOption(name: $0.key, count: $0.value.count) }
            .sorted { $0.name < $1.name }
    }

    var projectOptions: [FilterOption] {
        Dictionary(grouping: unarchivedTasks, by: { $0.projectText })
            .map { FilterOption(name: $0.key, count: $0.value.count) }
            .sorted { $0.name < $1.name }
    }

    var visibleTasks: [ObservedTask] {
        let freshness = freshness
        return executionOrder.ordered(filteredTasks.filter { view.matches($0.status(freshness: freshness)) })
    }

    /// Sidebar counts and list rows use the same scope before selecting a status view.
    private var filteredTasks: [ObservedTask] {
        unarchivedTasks
            .filter { projectFilter == nil || $0.projectText == projectFilter }
            .filter { hostFilter == nil || $0.hostText == hostFilter }
            .filter { task in
                guard let minutes = task.lastProgressMinutes else { return true }
                return minutes <= timeWindow.minutes
            }
            .filter { matchesSearch($0) }
    }

    private func matchesSearch(_ task: ObservedTask) -> Bool {
        let query = searchText.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return true }
        let haystack = [task.titleText, task.taskRef, task.stage, task.projectText, task.hostText]
            .joined(separator: " ")
            .lowercased()
        return haystack.contains(query.lowercased())
    }

    /// Active groups executions into “Needs attention” and the running tail, so blocked and
    /// failed work floats up. Other views are a single list.
    var groupedTasks: [TaskGroup] {
        let tasks = visibleTasks
        guard view == .active else {
            return tasks.isEmpty ? [] : [TaskGroup(id: view.rawValue, title: nil, tasks: tasks)]
        }
        let attention = tasks.filter { status(of: $0).isAttention }
        let rest = tasks.filter { !status(of: $0).isAttention }
        var groups: [TaskGroup] = []
        if !attention.isEmpty { groups.append(TaskGroup(id: "attention", title: "Needs attention", tasks: attention)) }
        if !rest.isEmpty {
            let title = isStaleSnapshot ? "Last known running" : "Running"
            groups.append(TaskGroup(id: "running", title: title, tasks: rest))
        }
        return groups
    }

    /// Which Observer page can still add rows to the current view: the Active and Blocked
    /// views are fed by the non-terminal page, the rest by the recent page.
    var hasMoreInCurrentView: Bool {
        switch view {
        case .active, .blocked: return activeNextOffset != nil
        case .recent, .completed, .failed: return recentNextOffset != nil
        }
    }

    var selectedStatus: MonitorStatus? { selected.map { status(of: $0) } }

    // MARK: - Selection

    var canGoBack: Bool { (navigationIndex ?? 0) > 0 }
    var canGoForward: Bool {
        guard let navigationIndex else { return false }
        return navigationIndex + 1 < navigationHistory.count
    }

    func goBack() {
        guard canGoBack, let navigationIndex else { return }
        openHistory(at: navigationIndex - 1)
    }

    func goForward() {
        guard canGoForward, let navigationIndex else { return }
        openHistory(at: navigationIndex + 1)
    }

    func openHistory(at index: Int) {
        guard navigationHistory.indices.contains(index) else { return }
        let entry = navigationHistory[index]
        navigationIndex = index
        view = entry.view
        projectFilter = entry.project
        hostFilter = entry.host
        timeWindow = entry.timeWindow
        executionOrder = entry.order
        searchText = entry.search
        beginSelection(entry.taskRef, executionRef: entry.executionRef, recordHistory: false)
    }

    private func recordVisit(_ ref: String, executionRef: String? = nil) {
        guard let task = allTasks.first(where: {
            $0.taskRef == ref && (executionRef == nil || $0.executionRef == executionRef)
        }), !isLocallyArchived(task) else { return }
        if let index = navigationIndex {
            let current = navigationHistory[index]
            if current.taskRef == ref && current.executionRef == task.executionRef
                && current.view == view && current.project == projectFilter
                && current.host == hostFilter && current.timeWindow == timeWindow
                && current.order == executionOrder && current.search == searchText {
                return
            }
            navigationHistory = Array(navigationHistory.prefix(index + 1))
        }
        navigationHistory.append(NavigationEntry(taskRef: ref, executionRef: task.executionRef,
                                                title: task.titleText, status: status(of: task), view: view,
                                                project: projectFilter, host: hostFilter,
                                                timeWindow: timeWindow, order: executionOrder, search: searchText))
        if navigationHistory.count > 50 { navigationHistory.removeFirst() }
        navigationIndex = navigationHistory.count - 1
    }

    private func removeHistory(for ref: String) {
        let remainingBeforeCursor = navigationHistory.prefix((navigationIndex ?? -1) + 1)
            .filter { $0.taskRef != ref }.count
        navigationHistory.removeAll { $0.taskRef == ref }
        navigationIndex = navigationHistory.isEmpty ? nil : max(0, remainingBeforeCursor - 1)
    }

    func selectionIndex() -> Int? {
        guard let selectedRef else { return nil }
        return visibleTasks.firstIndex {
            $0.taskRef == selectedRef &&
                (selectedExecutionRef == nil || $0.executionRef == selectedExecutionRef)
        }
    }

    func moveSelection(by delta: Int) {
        let tasks = visibleTasks
        guard !tasks.isEmpty else { return }
        let current = selectionIndex() ?? (delta > 0 ? -1 : tasks.count)
        let next = min(max(0, current + delta), tasks.count - 1)
        beginSelection(tasks[next].taskRef, executionRef: tasks[next].executionRef)
    }

    /// Highlights immediately (no wait on I/O) and loads the detail in the background.
    func beginSelection(_ ref: String, executionRef: String? = nil, recordHistory: Bool = true) {
        let task = allTasks.first(where: {
            $0.taskRef == ref && (executionRef == nil || $0.executionRef == executionRef)
        })
        let exactExecution = executionRef ?? task?.executionRef
        if recordHistory { recordVisit(ref, executionRef: exactExecution) }
        selectedRef = ref
        selectedExecutionRef = exactExecution
        if let task { seedSelection(task) }
        selectionToken += 1
        let token = selectionToken
        Task { await loadSelection(ref: ref, expectedExecutionRef: exactExecution, token: token) }
    }

    func clearSelection() {
        selectedRef = nil
        selectedExecutionRef = nil
        selected = nil
        selectedDetailIncomplete = false
        events = []
        eventCursor = nil
        eventHasMore = false
        eventCoverage = "UNKNOWN"
    }

    /// Awaits the detail load; used by tests and by callers that need the result.
    func select(_ ref: String, executionRef: String? = nil) async {
        let task = allTasks.first(where: {
            $0.taskRef == ref && (executionRef == nil || $0.executionRef == executionRef)
        })
        let exactExecution = executionRef ?? task?.executionRef
        recordVisit(ref, executionRef: exactExecution)
        selectedRef = ref
        selectedExecutionRef = exactExecution
        if let task { seedSelection(task) }
        selectionToken += 1
        await loadSelection(ref: ref, expectedExecutionRef: exactExecution, token: selectionToken)
    }

    private func seedSelection(_ task: ObservedTask) {
        selected = task
        selectedDetailIncomplete = false
        events = task.recentEvents.items
        eventCursor = task.recentEvents.nextCursor
        eventHasMore = task.recentEvents.hasMore
        eventCoverage = task.recentEvents.coverage
    }

    private func loadSelection(ref: String, expectedExecutionRef: String?, token: Int) async {
        guard let service, token == selectionToken else { return }
        if selected?.taskRef != ref || selected?.executionRef != expectedExecutionRef {
            if let snapshot = allTasks.first(where: {
                $0.taskRef == ref && (expectedExecutionRef == nil || $0.executionRef == expectedExecutionRef)
            }) {
                seedSelection(snapshot)
            } else {
                selected = nil; events = []; eventCursor = nil; eventHasMore = false
            }
        }
        do {
            let detail = try await service.task(ref)
            guard detail.taskRef == ref, detail.schemaVersion == "1",
                  detail.mutationBoundary.observerReadOnly,
                  detail.mutationBoundary.allowedActions.isEmpty else { throw MonitorError.incompatibleSchema }
            if let expectedExecutionRef, detail.executionRef != expectedExecutionRef {
                // Observer v1 has no historical execution-detail endpoint. Keep the
                // exact list snapshot selected and make the missing detail explicit;
                // never replace it with the current execution's events or result.
                guard token == selectionToken else { return }
                if let snapshot = allTasks.first(where: {
                    $0.taskRef == ref && $0.executionRef == expectedExecutionRef
                }) {
                    seedSelection(snapshot)
                    selectedDetailIncomplete = true
                }
                errorCategory = "Exact historical execution detail unavailable"
                return
            }
            guard token == selectionToken else { return }
            selected = detail
            selectedDetailIncomplete = false
            // Detail can be newer than the list snapshot. Keep the same execution's
            // row and inspector aligned without replacing a newer list observation.
            func refreshed(_ row: ObservedTask) -> ObservedTask {
                guard row.taskRef == detail.taskRef, row.executionRef == detail.executionRef,
                      let observed = TimestampParser.date(from: detail.timestamps.observedAt),
                      let previous = TimestampParser.date(from: row.timestamps.observedAt),
                      observed >= previous else { return row }
                return detail
            }
            active = Self.merge(active.map(refreshed))
            recent = Self.merge(recent.map(refreshed))
            let page = try await service.events(ref, after: nil)
            guard token == selectionToken else { return }
            try acceptEvents(page, ref: ref, reset: true)
        } catch {
            guard token == selectionToken else { return }
            errorCategory = Self.category(error)
        }
    }

    // MARK: - Sources

    func configure(endpoint: String) throws {
        guard let url = URL(string: endpoint) else { throw MonitorError.invalidEndpoint }
        let newService = try ObserverClient(baseURL: url)
        linkedDeviceID = nil
        linkedDeviceName = nil
        defaults.removeObject(forKey: "monitor.deviceID")
        defaults.removeObject(forKey: "monitor.deviceName")
        defaults.set(endpoint, forKey: "monitor.manualEndpoint")
        pollTask?.cancel()
        service = newService
        syntheticScenario = nil
        endpointText = endpoint
        defaults.set(endpoint, forKey: "monitor.endpoint") // Public endpoint only.
        resetForNewSource()
        start()
    }

    /// Called only after candidate health validation and successful Keychain storage.
    func configurePaired(endpoint: String, nodeID: String, name: String) throws {
        guard let url = URL(string: endpoint) else { throw MonitorError.invalidEndpoint }
        let client = try ObserverClient(baseURL: url, credentialAccount: "device:\(nodeID)")
        if linkedDeviceID == nil { defaults.set(endpointText, forKey: "monitor.manualEndpoint") }
        pollTask?.cancel()
        service = client
        syntheticScenario = nil
        linkedDeviceID = nodeID
        linkedDeviceName = name
        endpointText = endpoint
        defaults.set(endpoint, forKey: "monitor.endpoint")
        defaults.set(nodeID, forKey: "monitor.deviceID")
        defaults.set(name, forKey: "monitor.deviceName")
        resetForNewSource()
        start()
    }

    func disconnectPairedDevice() {
        pollTask?.cancel()
        service = nil
        linkedDeviceID = nil
        linkedDeviceName = nil
        syntheticScenario = nil
        endpointText = ""
        defaults.removeObject(forKey: "monitor.deviceID")
        defaults.removeObject(forKey: "monitor.deviceName")
        defaults.removeObject(forKey: "monitor.endpoint")
        resetForNewSource()
    }

    var manualEndpoint: String { defaults.string(forKey: "monitor.manualEndpoint") ?? endpointText }

    func credentialsChanged() {
        authFailed = false
        failureCount = 0
        start()
    }

    /// Synthetic acceptance mode. The scenario is served through the same read-only
    /// contract as the live Observer, and is always badged as SYNTHETIC DATA.
    func useSynthetic(_ scenario: SyntheticScenario) {
        pollTask?.cancel()
        syntheticScenario = scenario
        service = SyntheticObserverService(scenario: scenario)
        resetForNewSource()
        start()
    }

    func useLiveObserver() {
        pollTask?.cancel()
        syntheticScenario = nil
        if !endpointText.isEmpty, let url = URL(string: endpointText) {
            service = try? ObserverClient(baseURL: url, credentialAccount: credentialAccount)
        } else {
            service = nil
        }
        resetForNewSource()
        start()
    }

    private func resetForNewSource() {
        sourceToken += 1
        listRequestGeneration += 1
        selectionToken += 1
        isRefreshing = false
        navigationHistory = []
        navigationIndex = nil
        active = []
        recent = []
        selected = nil
        selectedDetailIncomplete = false
        selectedRef = nil
        selectedExecutionRef = nil
        events = []
        eventCursor = nil
        eventCoverage = "UNKNOWN"
        eventHasMore = false
        activeNextOffset = nil
        recentNextOffset = nil
        health = nil
        lastSuccessfulFetch = nil
        lastObservedAt = nil
        failed = false
        authFailed = false
        failureCount = 0
        errorCategory = nil
    }

    func start() {
        pollTask?.cancel()
        guard service != nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                guard let self else { return }
                let delay = self.retryDelay
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            }
        }
    }

    func stop() { pollTask?.cancel(); pollTask = nil }

    private var retryDelay: Double {
        if authFailed { return 60 }
        if failed { return min(60, Double(1 << min(failureCount, 6))) }
        if ProcessInfo.processInfo.isLowPowerModeEnabled { return 15 }
        return active.isEmpty ? 15 : 2
    }

    // MARK: - Refresh

    func refresh() async {
        guard let service, !isRefreshing else { return }
        let token = sourceToken
        listRequestGeneration += 1
        let requestGeneration = listRequestGeneration
        isRefreshing = true
        defer { if token == sourceToken { isRefreshing = false } }
        do {
            let newHealth = try await service.health()
            guard token == sourceToken, requestGeneration == listRequestGeneration else { return }
            guard newHealth.schemaVersion == "1", newHealth.readOnly else { throw MonitorError.incompatibleSchema }
            let activePage = try await service.tasks(active: true, offset: 0)
            let recentPage = try await service.tasks(active: false, offset: 0)
            guard token == sourceToken, requestGeneration == listRequestGeneration else { return }
            try validate(activePage)
            try validate(recentPage)
            health = newHealth
            active = Self.merge(activePage.items + (activePage.hasMore ? Array(active.dropFirst(50)) : []))
            recent = Self.merge(recentPage.items + (recentPage.hasMore ? Array(recent.dropFirst(50)) : []))
            activeNextOffset = active.count > 50 ? activeNextOffset : activePage.nextOffset
            recentNextOffset = recent.count > 50 ? recentNextOffset : recentPage.nextOffset
            lastObservedAt = activePage.observedAt
            lastSuccessfulFetch = Date()
            failed = false; authFailed = false; failureCount = 0; errorCategory = nil
            if selectedRef != nil { await reloadSelected(using: service) }
        } catch {
            guard token == sourceToken, requestGeneration == listRequestGeneration else { return }
            failed = true
            failureCount += 1
            if case MonitorError.server(401) = error { authFailed = true }
            errorCategory = Self.category(error)
        }
    }

    private func reloadSelected(using service: any ObserverServing) async {
        guard let ref = selectedRef else { return }
        selectionToken += 1
        await loadSelection(ref: ref, expectedExecutionRef: selectedExecutionRef, token: selectionToken)
    }

    func loadMoreEvents() async {
        guard let service, let ref = selectedRef, eventHasMore || eventCursor != nil else { return }
        let token = sourceToken
        do {
            let page = try await service.events(ref, after: eventCursor)
            guard token == sourceToken else { return }
            try acceptEvents(page, ref: ref, reset: false)
        } catch MonitorError.server(409) {
            guard token == sourceToken else { return }
            eventCursor = nil; events = []; eventHasMore = false
            selectionToken += 1
            await loadSelection(ref: ref, expectedExecutionRef: selectedExecutionRef, token: selectionToken)
        } catch { if token == sourceToken { errorCategory = Self.category(error) } }
    }

    private func acceptEvents(_ page: EventPage, ref: String, reset: Bool) throws {
        guard page.schemaVersion == "1", page.taskRef == ref else { throw MonitorError.incompatibleSchema }
        eventCoverage = page.coverage
        let scopedItems = page.items.filter { event in
            event.executionRef == nil || event.executionRef == selectedExecutionRef
        }
        events = Self.uniqueEvents((reset ? [] : events) + scopedItems)
        eventCursor = page.nextCursor
        eventHasMore = page.hasMore
        if var task = selected, task.taskRef == ref {
            task = task.withEvents(events, coverage: page.coverage, hasMore: page.hasMore)
            selected = task
        }
    }

    func loadMore() async {
        guard let service, !isRefreshing else { return }
        let token = sourceToken
        listRequestGeneration += 1
        let requestGeneration = listRequestGeneration
        let isActive = view == .active || view == .blocked
        guard let offset = isActive ? activeNextOffset : recentNextOffset else { return }
        do {
            let page = try await service.tasks(active: isActive, offset: offset)
            guard token == sourceToken, requestGeneration == listRequestGeneration else { return }
            try validate(page)
            if isActive {
                active = Self.merge(active + page.items)
                activeNextOffset = page.nextOffset
            } else {
                recent = Self.merge(recent + page.items)
                recentNextOffset = page.nextOffset
            }
        } catch { if token == sourceToken, requestGeneration == listRequestGeneration { errorCategory = Self.category(error) } }
    }

    /// Explicit overload retained for callers that page a specific list.
    func loadMore(active isActive: Bool) async {
        guard let service, !isRefreshing, let offset = isActive ? activeNextOffset : recentNextOffset else { return }
        let token = sourceToken
        listRequestGeneration += 1
        let requestGeneration = listRequestGeneration
        do {
            let page = try await service.tasks(active: isActive, offset: offset)
            guard token == sourceToken, requestGeneration == listRequestGeneration else { return }
            try validate(page)
            if isActive {
                active = Self.merge(active + page.items)
                activeNextOffset = page.nextOffset
            } else {
                recent = Self.merge(recent + page.items)
                recentNextOffset = page.nextOffset
            }
        } catch { if token == sourceToken, requestGeneration == listRequestGeneration { errorCategory = Self.category(error) } }
    }

    private func validate(_ page: TaskPage) throws {
        guard page.schemaVersion == "1", page.items.allSatisfy({
            $0.schemaVersion == "1" && $0.mutationBoundary.observerReadOnly &&
            $0.mutationBoundary.allowedActions.isEmpty
        }) else { throw MonitorError.incompatibleSchema }
    }

    private static func merge(_ tasks: [ObservedTask]) -> [ObservedTask] {
        var positions: [String: Int] = [:]
        var merged: [ObservedTask] = []
        for candidate in tasks {
            let key = candidate.executionIdentity
            guard let index = positions[key] else {
                positions[key] = merged.count
                merged.append(candidate)
                continue
            }
            if isNewer(candidate, than: merged[index]) { merged[index] = candidate }
        }
        return merged
    }

    private static func isNewer(_ candidate: ObservedTask, than current: ObservedTask) -> Bool {
        guard candidate.executionRef == current.executionRef else { return false }
        let candidateObserved = TimestampParser.date(from: candidate.timestamps.observedAt)
        let currentObserved = TimestampParser.date(from: current.timestamps.observedAt)
        if let candidateObserved, let currentObserved, candidateObserved != currentObserved {
            return candidateObserved > currentObserved
        }
        if candidateObserved != nil, currentObserved == nil { return true }
        if candidateObserved == currentObserved, candidate.monitorStatus != current.monitorStatus {
            let terminal: (MonitorStatus) -> Bool = {
                [.completed, .failed, .cancelled].contains($0)
            }
            if terminal(candidate.monitorStatus) != terminal(current.monitorStatus) {
                return terminal(candidate.monitorStatus)
            }
        }
        return false
    }
    private static func uniqueEvents(_ events: [ObserverEvent]) -> [ObserverEvent] {
        var seen = Set<String>()
        return events.filter { seen.insert($0.eventRef).inserted }
    }
    private static func category(_ error: Error) -> String {
        switch error {
        case MonitorError.server(401): return "Authentication required"
        case MonitorError.server(409): return "Event cursor reset"
        case MonitorError.server(404): return "Task not found"
        case MonitorError.server(503): return "Observer unavailable"
        case MonitorError.incompatibleSchema: return "Incompatible observer schema"
        case MonitorError.credentialUnavailable: return "Credential unavailable"
        default: return "Connection error"
        }
    }
}

extension ObservedTask {
    /// Replaces only the embedded event page; every other field stays exactly as observed.
    func withEvents(_ items: [ObserverEvent], coverage: String, hasMore: Bool) -> ObservedTask {
        ObservedTask(schemaVersion: schemaVersion, taskRef: taskRef, executionRef: executionRef,
                     project: project, title: title, host: host, state: state, stage: stage,
                     executionState: executionState, executionStage: executionStage, model: model,
                     reasoning: reasoning, currentActivity: currentActivity, blocker: blocker,
                     timestamps: timestamps, elapsedSeconds: elapsedSeconds, codexRunning: codexRunning,
                     retryRequired: retryRequired, mutationBoundary: mutationBoundary, routing: routing,
                     phases: phases, progressPercent: progressPercent, progressBasis: progressBasis,
                     recentEvents: EventPage(schemaVersion: recentEvents.schemaVersion, taskRef: taskRef,
                                             observedAt: recentEvents.observedAt, items: items,
                                             nextCursor: recentEvents.nextCursor, hasMore: hasMore,
                                             coverage: coverage),
                     hostOperations: hostOperations, hostOperationsHasMore: hostOperationsHasMore,
                     finalResult: finalResult, artifacts: artifacts, artifactsStatus: artifactsStatus,
                     menuState: menuState)
    }
}
