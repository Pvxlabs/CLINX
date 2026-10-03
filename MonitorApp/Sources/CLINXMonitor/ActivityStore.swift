import Foundation

@MainActor
final class ActivitySyncStatus: ObservableObject {
    @Published var lastFetch: Date?
}

@MainActor
final class ActivityStore: ObservableObject {
    @Published private(set) var messages: [ActivityMessage] = []
    @Published private(set) var notice: String?
    let syncStatus = ActivitySyncStatus()
    @Published private(set) var olderCursor: String?
    @Published private(set) var loading = false
    @Published private(set) var reachedLimit = false
    private(set) var cursor: String?
    private let taskRef: String
    private let executionRef: String
    private let service: (any ActivityServing)?
    private let sleep: @Sendable (UInt64) async throws -> Void
    private var terminal: Bool
    private var terminalPollsRemaining: Int
    private var terminalTailIncomplete = false
    private var lastRefreshSucceeded = false
    private var generation = 0
    private var requestInFlight = false

    init(taskRef: String, executionRef: String, service: (any ActivityServing)?, terminal: Bool = false,
         sleep: @escaping @Sendable (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0) }) {
        self.taskRef = taskRef
        self.executionRef = executionRef
        self.service = service
        self.sleep = sleep
        self.terminal = terminal
        terminalPollsRemaining = terminal ? 3 : 0
    }

    func updateTerminal(_ value: Bool) {
        guard terminal != value else { return }
        terminal = value
        terminalPollsRemaining = value ? 3 : 0
        terminalTailIncomplete = false
    }

    func run() async {
        generation += 1
        let runGeneration = generation
        while !Task.isCancelled && runGeneration == generation {
            await refresh()
            guard runGeneration == generation else { return }
            if terminal && hasTerminalMessage { return }
            if terminal && lastRefreshSucceeded && terminalPollsRemaining > 0 {
                terminalPollsRemaining -= 1
                if terminalPollsRemaining == 0 {
                    terminalTailIncomplete = true
                    notice = "Final activity is still being indexed; retrying in the background."
                }
            }
            do {
                let delay: UInt64 = terminal ?
                    (terminalTailIncomplete ? 10_000_000_000 : 1_000_000_000) :
                    (notice == nil ? 2_000_000_000 : 10_000_000_000)
                try await sleep(delay)
            }
            catch { return }
        }
    }

    func stop() { generation += 1 }

    func refresh(older: Bool = false) async {
        guard !requestInFlight else { return }
        guard !older || !reachedLimit else { return }
        guard !executionRef.isEmpty else { notice = "No exact execution is available for this task."; return }
        guard let service else { notice = "Execution feedback is unavailable for this source."; return }
        guard !older || olderCursor != nil else { return }
        let token = generation
        let wasInitial = cursor == nil
        lastRefreshSucceeded = false
        requestInFlight = true
        // Background delta polls should not invalidate the transcript twice per request.
        let showLoading = wasInitial || older
        if showLoading { loading = true }
        defer {
            requestInFlight = false
            if showLoading { loading = false }
        }
        do {
            let page = try await service.activity(taskRef, execution: executionRef,
                after: older ? nil : cursor, before: older ? olderCursor : nil)
            guard !Task.isCancelled, token == generation else { return }
            try accept(page, older: older, initial: wasInitial)
            lastRefreshSucceeded = page.availability == "AVAILABLE"
        } catch {
            guard !Task.isCancelled, token == generation else { return }
            if case MonitorError.server(409) = error {
                // Do not leave old messages visible after an identity/source reset.
                messages = []; cursor = nil; olderCursor = nil
                notice = "Activity changed. Reloading the current execution…"
            } else if case MonitorError.server(404) = error {
                notice = "Activity is unavailable on this Observer version."
            } else if case MonitorError.server(401) = error {
                notice = "Observer authorization is required."
            } else {
                notice = "Updates interrupted. Showing the last received activity."
            }
        }
    }

    func accept(_ page: ActivityPage, older: Bool = false, initial: Bool = false) throws {
        guard page.schemaVersion == "1", page.taskRef == taskRef, page.executionRef == executionRef,
              page.source == "CODEX_NATIVE_HISTORY" else { throw MonitorError.incompatibleSchema }
        guard page.availability == "AVAILABLE" else {
            messages = []; cursor = nil; olderCursor = nil
            notice = Self.unavailableText(page.reason)
            return
        }
        if !page.items.isEmpty {
            var merged = Dictionary(uniqueKeysWithValues: messages.map { ($0.id, $0) })
            for item in page.items where item.revision >= (merged[item.id]?.revision ?? -1) {
                merged[item.id] = item
            }
            let overMessageLimit = merged.count > 1000
            let byteCount = merged.values.reduce(into: 0) { $0 += $1.text.utf8.count }
            let overByteLimit = byteCount > 16 * 1024 * 1024
            guard !overMessageLimit, !overByteLimit else {
                reachedLimit = true
                notice = overMessageLimit
                    ? "1,000 messages loaded. Earlier feedback is capped; live final results remain available."
                    : "Activity reached its 16 MB display limit. Earlier feedback is capped; live final results remain available."
                // Keep a bounded final-result tail visible even when historical content
                // reaches the display budget. Live polling remains enabled for recovery.
                let results = merged.values.filter { $0.kind.lowercased() == "result" }
                    .sorted { ($0.ordinal, $0.id) < ($1.ordinal, $1.id) }
                if !results.isEmpty { messages = results }
                return
            }
            let ordered = merged.values.sorted { ($0.ordinal, $0.id) < ($1.ordinal, $1.id) }
            if ordered != messages {
                messages = ordered
            }
        }
        if !older { cursor = page.nextCursor }
        if (older || initial) && olderCursor != page.olderCursor { olderCursor = page.olderCursor }
        if hasTerminalMessage {
            terminalTailIncomplete = false
            notice = nil
        } else if !terminalTailIncomplete {
            notice = nil
        }
        syncStatus.lastFetch = TimestampParser.date(from: page.observedAt) ?? Date()
    }

    private var hasTerminalMessage: Bool {
        messages.contains { $0.kind.lowercased() == "result" }
    }

    private static func unavailableText(_ reason: String?) -> String {
        switch reason {
        case "EXACT_TURN_UNAVAILABLE": return "No provider turn is linked to this execution yet."
        case "NATIVE_HISTORY_MODE_UNSUPPORTED": return "This task uses a history format that Activity does not yet support."
        case "NATIVE_HOST_UNAVAILABLE": return "Execution feedback is unavailable on this host."
        case "PROVIDER_UNSUPPORTED": return "Execution feedback is unavailable for this provider."
        case "NATIVE_TURN_UNAVAILABLE": return "Waiting for this execution’s feedback to appear in the native history."
        default: return "Execution feedback is currently unavailable. Tool activity and results are shown when available."
        }
    }
}
