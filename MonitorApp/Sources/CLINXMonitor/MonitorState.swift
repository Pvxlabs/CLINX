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
    var progressLabel: String {
        guard let progressPercent, progressPercent.isFinite, (0.0...100.0).contains(progressPercent),
              progressBasis != "NO_PERSISTED_DENOMINATOR" else {
            return "Progress unknown (no persisted denominator)"
        }
        return "\(Int(progressPercent))%"
    }
}

enum ConnectionState: Equatable {
    case setup, loading, connected, degraded, stale, error(String)
}

@MainActor
final class MonitorStore: ObservableObject {
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

    private var service: (any ObserverServing)?
    private var pollTask: Task<Void, Never>?
    private var selectedRef: String?
    private var eventCursor: String?
    private var authFailed = false
    private var failureCount = 0
    private var failed = false

    init(service: (any ObserverServing)? = nil) {
        self.service = service
        endpointText = UserDefaults.standard.string(forKey: "monitor.endpoint") ?? ""
        if service == nil, let url = URL(string: endpointText), !endpointText.isEmpty {
            self.service = try? ObserverClient(baseURL: url)
        }
    }

    var connection: ConnectionState {
        guard service != nil else { return .setup }
        guard let lastSuccessfulFetch else { return failed ? .error(errorCategory ?? "Connection error") : .loading }
        let interval: TimeInterval = active.isEmpty ? 15 : 2
        if Date().timeIntervalSince(lastSuccessfulFetch) > max(3 * interval, 10) { return .stale }
        if failed { return .error(errorCategory ?? "Connection error") }
        if health?.status != "OK" { return .degraded }
        return .connected
    }

    var aggregate: DisplayState {
        let source = active.isEmpty ? Array(recent.prefix(1)) : active
        if source.contains(where: { [.blocked, .unknown].contains($0.displayState) }) { return .blocked }
        if source.contains(where: { $0.displayState == .running }) { return .running }
        if source.contains(where: { $0.displayState == .passed }) { return .passed }
        return .idle
    }

    func configure(endpoint: String) throws {
        guard let url = URL(string: endpoint) else { throw MonitorError.invalidEndpoint }
        let newService = try ObserverClient(baseURL: url)
        pollTask?.cancel()
        service = newService
        endpointText = endpoint
        UserDefaults.standard.set(endpoint, forKey: "monitor.endpoint") // Public endpoint only.
        active = []; recent = []; selected = nil; selectedRef = nil
        events = []; eventCursor = nil; eventCoverage = "UNKNOWN"; eventHasMore = false
        activeNextOffset = nil; recentNextOffset = nil
        health = nil; lastSuccessfulFetch = nil; lastObservedAt = nil
        failed = false; authFailed = false; failureCount = 0; errorCategory = nil
        start()
    }

    func credentialsChanged() {
        authFailed = false
        failureCount = 0
        start()
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

    func refresh() async {
        guard let service, !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let newHealth = try await service.health()
            guard newHealth.schemaVersion == "1", newHealth.readOnly else { throw MonitorError.incompatibleSchema }
            let activePage = try await service.tasks(active: true, offset: 0)
            let recentPage = try await service.tasks(active: false, offset: 0)
            try validate(activePage)
            try validate(recentPage)
            health = newHealth
            active = Self.unique(activePage.items + (activePage.hasMore ? Array(active.dropFirst(50)) : []))
            recent = Self.unique(recentPage.items + (recentPage.hasMore ? Array(recent.dropFirst(50)) : []))
            activeNextOffset = active.count > 50 ? activeNextOffset : activePage.nextOffset
            recentNextOffset = recent.count > 50 ? recentNextOffset : recentPage.nextOffset
            lastObservedAt = activePage.observedAt
            lastSuccessfulFetch = Date()
            failed = false; authFailed = false; failureCount = 0; errorCategory = nil
            if selectedRef != nil { try await loadSelected(using: service) }
        } catch {
            failed = true
            failureCount += 1
            if case MonitorError.server(401) = error { authFailed = true }
            errorCategory = Self.category(error)
        }
    }

    func select(_ ref: String) async {
        selectedRef = ref
        selected = nil; events = []; eventCursor = nil
        eventHasMore = false; eventCoverage = "UNKNOWN"
        guard let service else { return }
        do { try await loadSelected(using: service) }
        catch { errorCategory = Self.category(error) }
    }

    func clearSelection() { selectedRef = nil; selected = nil; events = []; eventCursor = nil }

    private func loadSelected(using service: any ObserverServing) async throws {
        guard let ref = selectedRef else { return }
        let detail = try await service.task(ref)
        guard detail.taskRef == ref, detail.schemaVersion == "1",
              detail.mutationBoundary.observerReadOnly,
              detail.mutationBoundary.allowedActions.isEmpty else { throw MonitorError.incompatibleSchema }
        selected = detail
        if eventCursor == nil {
            let page = try await service.events(ref, after: nil)
            try acceptEvents(page, ref: ref, reset: true)
        } else {
            await loadMoreEvents()
        }
    }

    func loadMoreEvents() async {
        guard let service, let ref = selectedRef, eventHasMore || eventCursor != nil else { return }
        do {
            let page = try await service.events(ref, after: eventCursor)
            try acceptEvents(page, ref: ref, reset: false)
        } catch MonitorError.server(409) {
            eventCursor = nil; events = []; eventHasMore = false
            do { try await loadSelected(using: service) }
            catch { errorCategory = Self.category(error) }
        } catch { errorCategory = Self.category(error) }
    }

    private func acceptEvents(_ page: EventPage, ref: String, reset: Bool) throws {
        guard page.schemaVersion == "1", page.taskRef == ref else { throw MonitorError.incompatibleSchema }
        eventCoverage = page.coverage
        events = Self.uniqueEvents((reset ? [] : events) + page.items)
        eventCursor = page.nextCursor
        eventHasMore = page.hasMore
    }

    func loadMore(active isActive: Bool) async {
        guard let service, let offset = isActive ? activeNextOffset : recentNextOffset else { return }
        do {
            let page = try await service.tasks(active: isActive, offset: offset)
            try validate(page)
            if isActive {
                active = Self.unique(active + page.items)
                activeNextOffset = page.nextOffset
            } else {
                recent = Self.unique(recent + page.items)
                recentNextOffset = page.nextOffset
            }
        } catch { errorCategory = Self.category(error) }
    }

    private func validate(_ page: TaskPage) throws {
        guard page.schemaVersion == "1", page.items.allSatisfy({
            $0.schemaVersion == "1" && $0.mutationBoundary.observerReadOnly &&
            $0.mutationBoundary.allowedActions.isEmpty
        }) else { throw MonitorError.incompatibleSchema }
    }

    private static func unique(_ tasks: [ObservedTask]) -> [ObservedTask] {
        var seen = Set<String>()
        return tasks.filter { seen.insert($0.taskRef).inserted }
    }
    private static func uniqueEvents(_ events: [ObserverEvent]) -> [ObserverEvent] {
        var seen = Set<String>()
        return events.filter { seen.insert($0.eventRef).inserted }
    }
    private static func category(_ error: Error) -> String {
        switch error {
        case MonitorError.server(401): return "Authentication required"
        case MonitorError.server(409): return "Event cursor reset"
        case MonitorError.server(503): return "Observer unavailable"
        case MonitorError.incompatibleSchema: return "Incompatible observer schema"
        case MonitorError.credentialUnavailable: return "Credential unavailable"
        default: return "Connection error"
        }
    }
}
