import Foundation

/// A task visit and the list context from which it was opened. Kept in this window's session.
struct NavigationEntry: Identifiable, Equatable {
    let id = UUID()
    let taskRef: String
    let title: String
    let status: MonitorStatus
    let view: MonitorView
    let project: String?
    let host: String?
    let timeWindow: TimeWindow
    let search: String
}
