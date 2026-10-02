import SwiftUI

// MARK: - The one status system
//
// Figma source of truth: "Design Principles" 04 (shape + label + color), the "Components"
// page (status system) and the "Developer Handoff" page (Status mapping — observer → UI)
// of `CLINX Monitor UI/UX Redesign`.
//
// One seven-state system is shared by row, pill, sidebar and inspector. Every state carries
// a unique glyph so state never depends on colour alone.

enum MonitorStatus: String, CaseIterable, Identifiable {
    case running, blocked, failed, completed, cancelled, stale, unknown

    var id: String { rawValue }

    var label: String {
        switch self {
        case .running: return "Running"
        case .blocked: return "Blocked"
        case .failed: return "Failed"
        case .completed: return "Completed"
        case .cancelled: return "Cancelled"
        case .stale: return "Stale"
        case .unknown: return "Unknown"
        }
    }

    var color: Color {
        switch self {
        case .running: return DS.Palette.running
        case .blocked: return DS.Palette.warning
        case .failed: return DS.Palette.error
        case .completed: return DS.Palette.success
        case .cancelled, .unknown: return DS.Palette.unknown
        case .stale: return DS.Palette.stale
        }
    }

    /// Attention floats up: blocked and failed sort first. It is expressed by the status
    /// glyph, the stage chip and the trailing label — the row carries no coloured edge.
    var isAttention: Bool { self == .blocked || self == .failed }

    /// Finished and attention-needed executions can be hidden locally and restored later.
    var canArchiveLocally: Bool { isAttention || self == .completed || self == .cancelled }

    /// Glyph shape, matching the design's unique-shape-per-state rule.
    var glyph: StatusGlyph {
        switch self {
        case .running: return .spinner
        case .blocked: return .circleMinus
        case .failed: return .squareCross
        case .completed: return .circleCheck
        case .cancelled: return .slash
        case .stale: return .triangle
        case .unknown: return .dashedRing
        }
    }
}

enum StatusGlyph {
    case spinner, circleMinus, squareCross, circleCheck, slash, triangle, dashedRing
}

// MARK: - Observer → UI status mapping

extension ObservedTask {
    /// A structured result only counts for the execution it was produced for.
    var exactResult: FinalResult? {
        guard let finalResult, finalResult.executionRef == executionRef else { return nil }
        return finalResult
    }

    /// A PASS is only surfaced for an execution that is genuinely completed: a structured
    /// PASS must never render as success on a task that is currently blocked or failed.
    var resultPassed: Bool { monitorStatus == .completed && exactResult?.status == "PASS" }

    private var claimsRunning: Bool {
        ["CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING", "FINALIZING",
         "CANCEL_REQUESTED", "CANCELLATION_PENDING"].contains(state) || codexRunning
    }

    private var hasHostFailure: Bool {
        hostOperations.contains { op in
            if let code = op.exitCode, code != 0 { return true }
            let result = op.resultState.uppercased()
            return result.contains("FAIL") || result.contains("ERROR")
        }
    }

    /// Developer-handoff mapping. Never infers success, never conflates states, and keeps a
    /// historical PASS from overriding a current BLOCKED/FAILED.
    var monitorStatus: MonitorStatus {
        if [state, executionState].contains(where: {
            ["BLOCKED", "RECOVERY_REQUIRED", "TRANSPORT_UNCERTAIN"].contains($0)
        })
            || exactResult?.status == "BLOCKED" { return .blocked }
        // Only canonical task/execution failure outranks retry-required. A failed
        // child command is evidence, not the final outcome of its parent execution.
        if state == "FAILED" || executionState == "FAILED" || exactResult?.status == "FAILED" { return .failed }
        if retryRequired { return .blocked }
        if claimsRunning { return .running }
        if ["CANCELLED", "STOPPED"].contains(state) || exactResult?.status == "CANCELLED" { return .cancelled }
        if ["COMPLETED", "IN_REVIEW"].contains(state) { return .completed }
        if hasHostFailure { return .failed }
        return .unknown
    }

    /// Staleness is a property of the snapshot, not of the task: `RUNNING + authority ≠ live`
    /// must never keep claiming “Running”. Stale is applied to every non-terminal state.
    func status(freshness: Freshness) -> MonitorStatus {
        let status = monitorStatus
        guard freshness != .current else { return status }
        switch status {
        case .running: return .stale
        case .blocked, .failed, .completed, .cancelled, .unknown: return status
        case .stale: return .stale
        }
    }
}

// MARK: - Freshness / authority

/// CURRENT, STALE and LAST KNOWN are always labelled (design principle 05).
enum Freshness: String {
    case current, stale, lastKnown

    var label: String {
        switch self {
        case .current: return "CURRENT"
        case .stale: return "STALE"
        case .lastKnown: return "LAST KNOWN"
        }
    }

    var color: Color {
        switch self {
        case .current: return DS.Palette.success
        case .stale: return DS.Palette.stale
        case .lastKnown: return DS.Palette.error
        }
    }

    var shortLabel: String { self == .current ? "Live" : label }
}

/// Connection · Authority · Sync — the toolbar's single status cluster.
enum ConnectionState7: String {
    case connected, degraded, offline

    var label: String { rawValue.capitalized }
    var color: Color {
        switch self {
        case .connected: return DS.Palette.success
        case .degraded: return DS.Palette.stale
        case .offline: return DS.Palette.error
        }
    }
}

/// The merged runtime status: environment and connection in one read-only indicator.
///
/// Figma component `RuntimeStatus` ships four states in two themes; the environment
/// segment names the data source (`SYNTHETIC` for fixtures) and the connection segment
/// the Observer link. Authority and last-sync are no longer rendered — the finalized
/// indicator is deliberately one line of information with no controls.
enum RuntimeStatusState: String, CaseIterable, Identifiable {
    case live, stale, offline, synthetic

    var id: String { rawValue }

    /// Observer connection + synthetic mode → the four finalized states.
    static func resolve(synthetic: Bool, connection: ConnectionState7) -> RuntimeStatusState {
        if synthetic { return .synthetic }
        switch connection {
        case .connected: return .live
        case .degraded: return .stale
        case .offline: return .offline
        }
    }

    var environment: String {
        switch self {
        case .live, .stale, .offline: return "P620"
        case .synthetic: return "SYNTHETIC"
        }
    }

    var connection: String {
        switch self {
        case .live: return "Connected"
        case .stale: return "Stale"
        case .offline: return "Offline"
        case .synthetic: return "Local"
        }
    }

    /// Status dot colour. Never the only signal: the connection word always names the state.
    var color: Color {
        switch self {
        case .live: return DS.Palette.success
        case .stale: return DS.Palette.stale
        case .offline: return DS.Palette.unknown
        case .synthetic: return DS.Palette.synthetic
        }
    }
}

@MainActor
extension MonitorStore {
    /// The docked indicator's state. Synthetic fixtures always read as synthetic — the
    /// fixture scenario, not the Observer link, is what the user must never mistake for live.
    var runtimeStatus: RuntimeStatusState {
        RuntimeStatusState.resolve(synthetic: syntheticScenario != nil, connection: connection7)
    }
}

enum AuthorityState: String {
    case live, stale, unknown

    var label: String { rawValue.uppercased() }
    var color: Color {
        switch self {
        case .live: return DS.Palette.textSecondary
        case .stale: return DS.Palette.stale
        case .unknown: return DS.Palette.unknown
        }
    }
}

// MARK: - Progress

/// User-facing progress. The legacy raw string (“Progress unknown (no persisted
/// denominator)”) never reaches the UI; the canonical reason lives in the tooltip.
enum ProgressPresentation: Equatable {
    case determinate(label: String, done: Int, total: Int)
    case indeterminate(label: String, done: Int?)
    case unavailable

    static let unavailableReason = "No canonical progress denominator is available."
}

extension ObservedTask {
    var progressPresentation: ProgressPresentation {
        // A live denominator must belong to the phase that is actually running.
        if let current = phases.first(where: { $0.state != "COMPLETED" && ($0.totalUnits ?? 0) > 0 && $0.completedUnits != nil }),
           let done = current.completedUnits, let total = current.totalUnits, total > 0 {
            return .determinate(label: current.title, done: done, total: total)
        }
        // Finished units only describe the whole execution once the execution is complete;
        // a blocked or failed task must never read as 100%.
        if monitorStatus == .completed,
           let last = phases.last(where: { ($0.totalUnits ?? 0) > 0 && $0.completedUnits != nil }),
           let done = last.completedUnits, let total = last.totalUnits, total > 0 {
            return .determinate(label: last.title, done: done, total: total)
        }
        if let progressPercent, progressPercent.isFinite, (0...100).contains(progressPercent),
           progressBasis != "NO_PERSISTED_DENOMINATOR" {
            return .determinate(label: stage, done: Int(progressPercent.rounded()), total: 100)
        }
        let completedUnits = phases.compactMap(\.completedUnits).reduce(0, +)
        if !phases.isEmpty {
            return .indeterminate(label: stage, done: completedUnits > 0 ? completedUnits : nil)
        }
        return .unavailable
    }
}

// MARK: - Views (sidebar navigation)

enum MonitorView: String, CaseIterable, Identifiable {
    case active, blocked, failed, recent, completed

    var id: String { rawValue }

    var label: String {
        switch self {
        case .active: return "Active"
        case .blocked: return "Blocked"
        case .failed: return "Failed"
        case .recent: return "Recent"
        case .completed: return "Completed"
        }
    }

    /// ⌘1 – ⌘5, per the Developer Handoff keyboard table.
    var shortcutIndex: Int {
        switch self {
        case .active: return 1
        case .blocked: return 2
        case .failed: return 3
        case .recent: return 4
        case .completed: return 5
        }
    }

    /// The status a view's count carries when it needs attention. One shared rule for all
    /// five views — never a per-view special case — and only colour: the count is plain
    /// text, so attention can never widen it or push the digits off the alignment column.
    var countTint: MonitorStatus? {
        switch self {
        case .blocked: return .blocked
        case .failed: return .failed
        case .active, .recent, .completed: return nil
        }
    }

    func matches(_ status: MonitorStatus) -> Bool {
        switch self {
        case .active: return status == .running || status == .blocked || status == .stale
        case .blocked: return status == .blocked
        case .failed: return status == .failed
        case .recent: return true
        case .completed: return status == .completed || status == .cancelled
        }
    }
}

// MARK: - Timeline presentation

enum TimelineKind {
    case start, connect, dispatch, done, deliver, writeback, progress, error, warn, observe

    /// Glyph + colour per the design's EVENT_ICON table.
    var symbol: String {
        switch self {
        case .start: return "waveform.path.ecg"
        case .connect: return "powerplug"
        case .dispatch: return "display"
        case .done, .writeback: return "checkmark"
        case .deliver: return "shippingbox"
        case .progress: return "chevron.right"
        case .error: return "xmark"
        case .warn: return "clock"
        case .observe: return "circle.fill"
        }
    }

    var color: Color {
        switch self {
        case .start, .connect, .dispatch, .observe: return DS.Palette.textSecondary
        case .done, .deliver, .writeback: return DS.Palette.success
        case .progress: return DS.Palette.running
        case .error: return DS.Palette.error
        case .warn: return DS.Palette.warning
        }
    }
}

struct TimelineItem: Identifiable, Equatable {
    let id: String
    let time: String
    let kind: TimelineKind
    let title: String
    let meta: String?
    let mono: String?
    var occurredAt: Date? = nil
}

/// Observer event kinds → human timeline titles.
///
/// The Observer wire model deliberately carries no event payload, so a timeline row shows
/// the canonical event reference as mono metadata instead of free text (docs/CLINX_MONITOR.md
/// forbids exposing raw stdout/stderr/argv/reasoning).
enum EventPresentation {
    static func kind(for observerKind: String) -> TimelineKind {
        switch observerKind {
        case "V1SnapshotBaselineImported": return .start
        case "V1ExecutionClaimObserved", "V1UnattributedExecutionClaimObserved": return .dispatch
        case "V1HostExecutionStartedObserved": return .dispatch
        case "V1ExecutionProgressObserved": return .progress
        // Recording a fact is not a success: these two are neutral observations, so a
        // blocked or failed execution never ends on a green check it did not earn.
        case "V1HostEvidenceObserved": return .observe
        case "V1ExecutionResultPersistedObserved": return .writeback
        case "V1TerminalStateObserved": return .observe
        case "V1LeaseReleasedObserved", "V1UnattributedLeaseReleasedObserved": return .deliver
        default: return .progress
        }
    }

    static func title(for observerKind: String) -> String {
        switch observerKind {
        case "V1SnapshotBaselineImported": return "Execution baseline imported"
        case "V1ExecutionClaimObserved": return "Execution claimed"
        case "V1UnattributedExecutionClaimObserved": return "Execution claimed without attribution"
        case "V1HostExecutionStartedObserved": return "Host command dispatched"
        case "V1ExecutionProgressObserved": return "Progress recorded"
        case "V1HostEvidenceObserved": return "Host evidence recorded"
        case "V1ExecutionResultPersistedObserved": return "Result persisted"
        case "V1TerminalStateObserved": return "Terminal state observed"
        case "V1LeaseReleasedObserved": return "Lease released"
        case "V1UnattributedLeaseReleasedObserved": return "Lease released without attribution"
        default: return observerKind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}
