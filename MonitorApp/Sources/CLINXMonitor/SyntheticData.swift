import Foundation

// MARK: - Synthetic acceptance data
//
// Figma source of truth: `src/monitor/data.ts` in `CLINX Monitor UI/UX Redesign`.
// The scenario names, task set, blockers and timelines mirror that fixture so the synthetic
// acceptance screenshots can be compared against the design frame by frame.
//
// This data is served through the same `ObserverServing` contract as the live Observer and
// is always rendered behind the purple SYNTHETIC DATA identity — it can never be mistaken
// for P620 live data.

enum SyntheticScenario: String, CaseIterable, Identifiable {
    case healthy, running, blocked, failed, stale, offline, long, empty

    var id: String { rawValue }

    var label: String {
        switch self {
        case .healthy: return "Healthy"
        case .running: return "Running"
        case .blocked: return "Blocked"
        case .failed: return "Failed"
        case .stale: return "Stale"
        case .offline: return "Offline"
        case .long: return "Long Content"
        case .empty: return "Empty"
        }
    }

    var connection: ConnectionState7 {
        switch self {
        case .stale: return .degraded
        case .offline: return .offline
        default: return .connected
        }
    }

    var authority: AuthorityState {
        switch self {
        case .stale: return .stale
        case .offline: return .unknown
        default: return .live
        }
    }

    /// The authority liveness the Observer itself would report.
    var authorityLiveness: String {
        switch self {
        case .stale: return "STALE"
        case .offline: return "UNKNOWN"
        default: return "LIVE"
        }
    }

    var freshness: Freshness {
        switch self {
        case .stale: return .stale
        case .offline: return .lastKnown
        default: return .current
        }
    }

    var syncText: String {
        switch self {
        case .stale: return "4m ago"
        case .offline: return "12m ago"
        default: return "12s ago"
        }
    }

    var connectivityNote: String? {
        switch self {
        case .stale: return "Observer degraded · Snapshot is 4m old"
        case .offline: return "Observer unavailable"
        default: return nil
        }
    }

    var connectivityDetail: String? {
        switch self {
        case .stale: return "Some execution events may be incomplete. Running states shown as Stale."
        case .offline: return "Last successful sync 12m ago (09:06:41). Showing last known data."
        default: return nil
        }
    }

    var connectivityTail: String? {
        switch self {
        case .stale: return "next sync in 18s"
        case .offline: return "retrying every 30s"
        default: return nil
        }
    }
}

// MARK: - Fixture construction

private enum Fixture {
    static let iso: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    static func stamp(minutesAgo: Int, secondsAgo: Int = 0, now: Date = Date()) -> String {
        iso.string(from: now.addingTimeInterval(-Double(minutesAgo * 60 + secondsAgo)))
    }

    static let boundary = MutationBoundary(observerReadOnly: true, allowedActions: [],
                                           executionOperationClasses: nil, executionSurface: nil,
                                           authorityStatus: "READ_ONLY")

    static func event(_ ref: String, _ kind: String, execution: String, minutesAgo: Int,
                      secondsAgo: Int = 0, ordinal: Int) -> ObserverEvent {
        ObserverEvent(cursor: String(format: "%016x.%d", ordinal, ordinal),
                      eventRef: ref, kind: kind, executionRef: execution,
                      occurredAt: stamp(minutesAgo: minutesAgo, secondsAgo: secondsAgo),
                      recordedAt: stamp(minutesAgo: minutesAgo, secondsAgo: secondsAgo))
    }

    /// The design's happy chain: started → provider connected → host dispatched → host
    /// completed → result delivered.
    static func chain(execution: String, startedMinutesAgo: Int, startIndex: Int = 1) -> [ObserverEvent] {
        let base = startedMinutesAgo
        return [
            event("evt_start", "V1SnapshotBaselineImported", execution: execution, minutesAgo: base, ordinal: startIndex),
            event("evt_claim", "V1ExecutionClaimObserved", execution: execution, minutesAgo: base, secondsAgo: -2, ordinal: startIndex + 1),
            event("evt_host", "V1HostExecutionStartedObserved", execution: execution, minutesAgo: base, secondsAgo: -4, ordinal: startIndex + 2),
            event("evt_evidence", "V1HostEvidenceObserved", execution: execution, minutesAgo: base, secondsAgo: -8, ordinal: startIndex + 3),
            event("evt_result", "V1ExecutionResultPersistedObserved", execution: execution, minutesAgo: base, secondsAgo: -9, ordinal: startIndex + 4),
        ]
    }
}

// MARK: - Task fixtures

enum SyntheticTasks {

    static func all(now: Date = Date()) -> [ObservedTask] {
        [
            running(now), blockedToolNamespace(now), runningImplementation(now), runningPlanning(now),
            blockedRateLimit(now), failedHostExit(now), completedDesignTokens(now), completedAudit(now),
            cancelledReindex(now), completedReleaseNotes(now), completedPrune(now), completedSnapshot(now),
        ]
    }

    static func longContent(now: Date = Date()) -> [ObservedTask] {
        all(now: now).prefix(4).enumerated().map { index, task in
            let tail = String(repeating: "长任务标题 Long task title ", count: 5)
            let blocker = task.blocker.map { existing in
                Blocker(code: existing.code,
                        message: String(repeating: "SYNTHETIC_BLOCKER: 长 blocker 用于检查换行和滚动。", count: 12))
            }
            return copy(task,
                        taskRef: "exec_fixture_\(index)",
                        title: "SYNTHETIC \(task.monitorStatus.rawValue.uppercased()) — \(tail)".trimmingCharacters(in: .whitespaces),
                        project: "fixture",
                        blocker: blocker)
        }
    }

    // swiftlint:disable:next function_parameter_count
    private static func make(taskRef: String, executionRef: String, title: String, project: String,
                             host: String, state: String, stage: String, executionState: String,
                             activity: String, startedMinutesAgo: Int, lastProgressMinutesAgo: Int,
                             provider: String, phases: [Phase], blocker: Blocker?,
                             events: [ObserverEvent], operations: [HostOperation],
                             finalResult: FinalResult?, codexRunning: Bool,
                             retryRequired: Bool = false, now: Date) -> ObservedTask {
        ObservedTask(
            schemaVersion: "1", taskRef: taskRef, executionRef: executionRef, project: project,
            title: title, host: host, state: state, stage: stage, executionState: executionState,
            executionStage: stage,
            model: ModelIdentity(logical: provider == "claude" ? "claude-opus-5" : "gpt-5-codex",
                                 resolved: provider == "claude" ? "claude-opus-5-5" : "gpt-5-codex"),
            reasoning: "medium",
            currentActivity: CurrentActivity(kind: "PERSISTED_STAGE", label: activity,
                                             observedAt: Fixture.stamp(minutesAgo: lastProgressMinutesAgo, now: now)),
            blocker: blocker,
            timestamps: TaskTimestamps(createdAt: Fixture.stamp(minutesAgo: startedMinutesAgo, now: now),
                                       updatedAt: Fixture.stamp(minutesAgo: lastProgressMinutesAgo, now: now),
                                       lastProgressAt: Fixture.stamp(minutesAgo: lastProgressMinutesAgo, now: now),
                                       startedAt: Fixture.stamp(minutesAgo: startedMinutesAgo, now: now),
                                       completedAt: finalResult != nil ? Fixture.stamp(minutesAgo: lastProgressMinutesAgo, now: now) : nil,
                                       observedAt: Fixture.stamp(minutesAgo: 0, secondsAgo: 12, now: now)),
            elapsedSeconds: startedMinutesAgo * 60,
            codexRunning: codexRunning, retryRequired: retryRequired,
            mutationBoundary: Fixture.boundary,
            routing: Routing(host: RouteComponent(identifier: host, status: "READY"),
                             surface: RouteComponent(identifier: provider, status: "READY"),
                             provider: RouteComponent(identifier: provider, status: "READY"),
                             transport: RouteComponent(identifier: "tailnet", status: "READY")),
            phases: phases,
            progressPercent: nil,
            progressBasis: phases.isEmpty ? "NO_PERSISTED_DENOMINATOR" : "PERSISTED_PHASE_UNITS",
            recentEvents: EventPage(schemaVersion: nil, taskRef: taskRef, observedAt: nil,
                                    items: events, nextCursor: events.last?.cursor ?? "",
                                    hasMore: false, coverage: "PARTIAL"),
            hostOperations: operations, hostOperationsHasMore: false,
            finalResult: finalResult, artifacts: [],
            artifactsStatus: "EMPTY", menuState: "READ_ONLY")
    }

    private static func phase(_ ref: String, _ title: String, _ state: String,
                              done: Int?, total: Int?) -> Phase {
        Phase(phaseRef: ref, title: title, state: state, completedUnits: done, totalUnits: total,
              unit: "steps", evidenceRef: ref, progressPercent: nil)
    }

    private static func copy(_ task: ObservedTask, taskRef: String? = nil, title: String? = nil,
                             project: String? = nil, blocker: Blocker?) -> ObservedTask {
        ObservedTask(
            schemaVersion: task.schemaVersion, taskRef: taskRef ?? task.taskRef,
            executionRef: task.executionRef, project: project ?? task.project, title: title ?? task.title,
            host: task.host, state: task.state, stage: task.stage, executionState: task.executionState,
            executionStage: task.executionStage, model: task.model, reasoning: task.reasoning,
            currentActivity: task.currentActivity, blocker: blocker, timestamps: task.timestamps,
            elapsedSeconds: task.elapsedSeconds, codexRunning: task.codexRunning,
            retryRequired: task.retryRequired, mutationBoundary: task.mutationBoundary,
            routing: task.routing, phases: task.phases, progressPercent: task.progressPercent,
            progressBasis: task.progressBasis, recentEvents: task.recentEvents,
            hostOperations: task.hostOperations, hostOperationsHasMore: task.hostOperationsHasMore,
            finalResult: task.finalResult, artifacts: task.artifacts,
            artifactsStatus: task.artifactsStatus, menuState: task.menuState)
    }

    // MARK: individual fixtures

    private static func running(_ now: Date) -> ObservedTask {
        make(taskRef: "task_abb594e2c1", executionRef: "exec_abb594e2c1", title: "Deploy ORION OPS vNext",
             project: "ORION", host: "p620", state: "CODEX_RUNNING", stage: "VALIDATION",
             executionState: "CODEX_RUNNING", activity: "CODEX_RUNNING", startedMinutesAgo: 18,
             lastProgressMinutesAgo: 2, provider: "codex",
             phases: [phase("p1", "Plan", "COMPLETED", done: 6, total: 6),
                      phase("p2", "Build", "COMPLETED", done: 6, total: 6),
                      phase("p3", "Validation", "RUNNING", done: 17, total: 24)],
             blocker: nil,
             events: Fixture.chain(execution: "exec_abb594e2c1", startedMinutesAgo: 18)
                 + [Fixture.event("evt_progress_1", "V1ExecutionProgressObserved", execution: "exec_abb594e2c1", minutesAgo: 14, ordinal: 6),
                    Fixture.event("evt_progress_2", "V1ExecutionProgressObserved", execution: "exec_abb594e2c1", minutesAgo: 7, ordinal: 7),
                    Fixture.event("evt_progress_3", "V1ExecutionProgressObserved", execution: "exec_abb594e2c1", minutesAgo: 2, ordinal: 8)],
             operations: [HostOperation(hostExecutionRef: "hostexec_7f13a0", executionRef: "exec_abb594e2c1",
                                        host: "p620", surface: "host", operationClass: "read_only",
                                        capability: "shell", operation: "swift test", startedAt: Fixture.stamp(minutesAgo: 17, now: now),
                                        completedAt: Fixture.stamp(minutesAgo: 16, secondsAgo: 45, now: now), durationMs: 1800,
                                        exitCode: 0, resultState: "SUCCEEDED", timedOut: false),
                          HostOperation(hostExecutionRef: "hostexec_7f13b1", executionRef: "exec_abb594e2c1",
                                        host: "p620", surface: "host", operationClass: "read_only",
                                        capability: "shell", operation: "swift build", startedAt: Fixture.stamp(minutesAgo: 16, now: now),
                                        completedAt: Fixture.stamp(minutesAgo: 15, secondsAgo: 30, now: now), durationMs: 2250,
                                        exitCode: 0, resultState: "SUCCEEDED", timedOut: false)],
             finalResult: nil, codexRunning: true, now: now)
    }

    private static func blockedToolNamespace(_ now: Date) -> ObservedTask {
        make(taskRef: "task_4c01d8f77a", executionRef: "exec_4c01d8f77a", title: "CLINX Monitor Phase 2",
             project: "CLINX", host: "p620", state: "BLOCKED", stage: "PROVIDER_DELIVERY",
             executionState: "BLOCKED", activity: "COMPLETED", startedMinutesAgo: 18,
             lastProgressMinutesAgo: 6, provider: "codex",
             phases: [phase("p1", "Plan", "COMPLETED", done: 4, total: 4),
                      phase("p2", "Provider delivery", "BLOCKED", done: nil, total: nil)],
             blocker: Blocker(code: "E_TOOL_NAMESPACE",
                              message: "Unsupported dynamic tool namespace: clinx"),
             events: Array(Fixture.chain(execution: "exec_4c01d8f77a", startedMinutesAgo: 18).prefix(3))
                 + [Fixture.event("evt_error_1", "V1TerminalStateObserved", execution: "exec_4c01d8f77a", minutesAgo: 6, ordinal: 6)],
             operations: [], finalResult: nil, codexRunning: false, now: now)
    }

    private static func runningImplementation(_ now: Date) -> ObservedTask {
        make(taskRef: "task_91ad0c3e55", executionRef: "exec_91ad0c3e55",
             title: "Migrate worker queue to durable streams", project: "CLINX", host: "p620",
             state: "CODEX_RUNNING", stage: "IMPLEMENTATION", executionState: "CODEX_RUNNING",
             activity: "CODEX_RUNNING", startedMinutesAgo: 42, lastProgressMinutesAgo: 0, provider: "codex",
             phases: [phase("p1", "Plan", "COMPLETED", done: 4, total: 4),
                      phase("p2", "Implementation", "RUNNING", done: 9, total: nil)],
             blocker: nil,
             events: Fixture.chain(execution: "exec_91ad0c3e55", startedMinutesAgo: 42)
                 + [Fixture.event("evt_progress_9", "V1ExecutionProgressObserved", execution: "exec_91ad0c3e55", minutesAgo: 0, secondsAgo: 14, ordinal: 6)],
             operations: [], finalResult: nil, codexRunning: true, now: now)
    }

    private static func runningPlanning(_ now: Date) -> ObservedTask {
        make(taskRef: "task_c77e2a9b10", executionRef: "exec_c77e2a9b10",
             title: "Rotate ORION staging TLS certificates", project: "ORION", host: "m2-build",
             state: "CODEX_RUNNING", stage: "PLANNING", executionState: "CODEX_RUNNING",
             activity: "CODEX_RUNNING", startedMinutesAgo: 3, lastProgressMinutesAgo: 0, provider: "codex",
             phases: [phase("p1", "Planning", "RUNNING", done: nil, total: nil)],
             blocker: nil, events: Fixture.chain(execution: "exec_c77e2a9b10", startedMinutesAgo: 3),
             operations: [], finalResult: nil, codexRunning: true, now: now)
    }

    private static func blockedRateLimit(_ now: Date) -> ObservedTask {
        make(taskRef: "task_2bd4f61e08", executionRef: "exec_2bd4f61e08",
             title: "Refactor provider adapter retries into policy module", project: "CLINX",
             host: "p620", state: "BLOCKED", stage: "WAITING_PROVIDER", executionState: "BLOCKED",
             activity: "IDLE", startedMinutesAgo: 27, lastProgressMinutesAgo: 11, provider: "claude",
             phases: [], blocker: Blocker(code: "E_PROVIDER_429",
                                          message: "HTTP 429 from provider; next window 09:24"),
             events: Fixture.chain(execution: "exec_2bd4f61e08", startedMinutesAgo: 27)
                 + [Fixture.event("evt_429", "V1TerminalStateObserved", execution: "exec_2bd4f61e08", minutesAgo: 11, ordinal: 6)],
             operations: [], finalResult: nil, codexRunning: false, now: now)
    }

    private static func failedHostExit(_ now: Date) -> ObservedTask {
        make(taskRef: "task_e0f3a1c2d9", executionRef: "exec_e0f3a1c2d9",
             title: "Backfill ORION audit log partitions (2026-Q3)", project: "ORION", host: "p620",
             state: "FAILED", stage: "HOST_EXECUTION", executionState: "FAILED", activity: "EXITED",
             startedMinutesAgo: 32, lastProgressMinutesAgo: 22, provider: "codex",
             phases: [phase("p1", "Plan", "COMPLETED", done: 3, total: 3),
                      phase("p2", "Host execution", "FAILED", done: nil, total: nil)],
             blocker: Blocker(code: "EXIT_1",
                              message: "psql: relation \"audit_2026_q3\" does not exist"),
             events: Array(Fixture.chain(execution: "exec_e0f3a1c2d9", startedMinutesAgo: 32).prefix(3))
                 + [Fixture.event("evt_exit", "V1HostEvidenceObserved", execution: "exec_e0f3a1c2d9", minutesAgo: 22, ordinal: 6),
                    Fixture.event("evt_terminal", "V1TerminalStateObserved", execution: "exec_e0f3a1c2d9", minutesAgo: 22, secondsAgo: -1, ordinal: 7)],
             operations: [HostOperation(hostExecutionRef: "hostexec_51ce02", executionRef: "exec_e0f3a1c2d9",
                                        host: "p620", surface: "host", operationClass: "read_only",
                                        capability: "shell", operation: "scripts/backfill.sh", startedAt: Fixture.stamp(minutesAgo: 26, now: now),
                                        completedAt: Fixture.stamp(minutesAgo: 22, now: now), durationMs: 252000,
                                        exitCode: 1, resultState: "FAILED", timedOut: false)],
             finalResult: nil, codexRunning: false, retryRequired: true, now: now)
    }

    private static func completedDesignTokens(_ now: Date) -> ObservedTask {
        completed(taskRef: "task_7a6b5c4d3e", executionRef: "exec_7a6b5c4d3e",
                  title: "Add SF Mono tokens to ORION design system", project: "ORION", host: "m2-build",
                  stage: "DONE", startedMinutesAgo: 44, lastProgressMinutesAgo: 31, provider: "codex",
                  done: 14, total: 14, now: now)
    }

    private static func completedAudit(_ now: Date) -> ObservedTask {
        completed(taskRef: "task_19fe2033ab", executionRef: "exec_19fe2033ab", title: "Nightly dependency audit",
                  project: "CLINX", host: "p620", stage: "DONE", startedMinutesAgo: 140,
                  lastProgressMinutesAgo: 120, provider: "codex", done: 6, total: 6, now: now)
    }

    private static func completedReleaseNotes(_ now: Date) -> ObservedTask {
        completed(taskRef: "task_0d9c8b7a65", executionRef: "exec_0d9c8b7a65",
                  title: "Generate CLINX release notes 0.14", project: "CLINX", host: "m2-build",
                  stage: "DONE", startedMinutesAgo: 190, lastProgressMinutesAgo: 180,
                  provider: "claude", done: 3, total: 3, now: now)
    }

    private static func completedPrune(_ now: Date) -> ObservedTask {
        completed(taskRef: "task_5544aa1100", executionRef: "exec_5544aa1100",
                  title: "Prune stale preview environments", project: "ORION", host: "p620",
                  stage: "DONE", startedMinutesAgo: 241, lastProgressMinutesAgo: 240,
                  provider: "codex", done: 2, total: 2, now: now)
    }

    private static func completedSnapshot(_ now: Date) -> ObservedTask {
        completed(taskRef: "task_aa00bb11cc", executionRef: "exec_aa00bb11cc",
                  title: "Snapshot ORION prod schema for drift check", project: "ORION", host: "p620",
                  stage: "DONE", startedMinutesAgo: 301, lastProgressMinutesAgo: 300,
                  provider: "codex", done: 1, total: 1, now: now)
    }

    private static func completed(taskRef: String, executionRef: String, title: String, project: String,
                                  host: String, stage: String, startedMinutesAgo: Int,
                                  lastProgressMinutesAgo: Int, provider: String, done: Int, total: Int,
                                  now: Date) -> ObservedTask {
        make(taskRef: taskRef, executionRef: executionRef, title: title, project: project, host: host,
             state: "COMPLETED", stage: stage, executionState: "COMPLETED", activity: "COMPLETED",
             startedMinutesAgo: startedMinutesAgo, lastProgressMinutesAgo: lastProgressMinutesAgo,
             provider: provider,
             phases: [phase("p1", "Complete", "COMPLETED", done: done, total: total)],
             blocker: nil,
             events: Fixture.chain(execution: executionRef, startedMinutesAgo: startedMinutesAgo)
                 + [Fixture.event("evt_writeback", "V1ExecutionResultPersistedObserved", execution: executionRef, minutesAgo: lastProgressMinutesAgo, ordinal: 6)],
             operations: [], finalResult: nil, codexRunning: false, now: now)
    }

    private static func cancelledReindex(_ now: Date) -> ObservedTask {
        make(taskRef: "task_b3c4d5e6f7", executionRef: "exec_b3c4d5e6f7",
             title: "Reindex ORION search embeddings", project: "ORION", host: "p620", state: "CANCELLED",
             stage: "CANCELLED", executionState: "CANCELLED", activity: "STOPPED", startedMinutesAgo: 70,
             lastProgressMinutesAgo: 64, provider: "codex", phases: [],
             blocker: nil,
             events: Array(Fixture.chain(execution: "exec_b3c4d5e6f7", startedMinutesAgo: 70).prefix(3))
                 + [Fixture.event("evt_cancel", "V1TerminalStateObserved", execution: "exec_b3c4d5e6f7", minutesAgo: 64, ordinal: 6)],
             operations: [], finalResult: nil, codexRunning: false, now: now)
    }
}

// MARK: - Synthetic service

/// Serves the fixture through the canonical read-only contract.
struct SyntheticObserverService: ObserverServing {
    let scenario: SyntheticScenario
    private let pageSize = 6

    init(scenario: SyntheticScenario) { self.scenario = scenario }

    private var tasks: [ObservedTask] {
        switch scenario {
        case .empty: return []
        case .long: return SyntheticTasks.longContent()
        case .healthy:
            return SyntheticTasks.all().filter { task in
                let status = task.monitorStatus
                return status != .blocked && status != .failed
            }
        default: return SyntheticTasks.all()
        }
    }

    private func isActive(_ task: ObservedTask) -> Bool {
        switch task.monitorStatus {
        case .running, .blocked: return true
        default: return false
        }
    }

    func health() async throws -> ObserverHealth {
        ObserverHealth(schemaVersion: "1", status: scenario == .offline ? "UNAVAILABLE" : "OK",
                       readOnly: true, observedAt: Fixture.stamp(minutesAgo: 0, secondsAgo: 12),
                       authorityLiveness: scenario.authorityLiveness, authMode: "bearer",
                       eventsTransport: "polling")
    }

    func tasks(active: Bool, offset: Int) async throws -> TaskPage {
        let filtered = tasks.filter { isActive($0) == active }
        let start = min(max(0, offset), filtered.count)
        let end = min(start + pageSize, filtered.count)
        return TaskPage(schemaVersion: "1", observedAt: Fixture.stamp(minutesAgo: 0, secondsAgo: 12),
                        items: Array(filtered[start..<end]),
                        nextOffset: end < filtered.count ? end : nil,
                        hasMore: end < filtered.count)
    }

    func task(_ ref: String) async throws -> ObservedTask {
        guard let match = tasks.first(where: { $0.taskRef == ref }) else {
            throw MonitorError.server(404)
        }
        return match
    }

    func events(_ ref: String, after: String?) async throws -> EventPage {
        guard let match = tasks.first(where: { $0.taskRef == ref }) else {
            throw MonitorError.server(404)
        }
        let all = match.recentEvents.items
        let slice: [ObserverEvent]
        if let after, let index = all.firstIndex(where: { $0.cursor == after }) {
            slice = Array(all[(index + 1)...])
        } else {
            slice = all
        }
        return EventPage(schemaVersion: "1", taskRef: ref, observedAt: Fixture.stamp(minutesAgo: 0, secondsAgo: 12),
                         items: slice, nextCursor: slice.last?.cursor ?? match.recentEvents.nextCursor,
                         hasMore: false, coverage: "PARTIAL")
    }
}
