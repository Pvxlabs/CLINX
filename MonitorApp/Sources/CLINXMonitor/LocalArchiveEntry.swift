import Foundation

/// A local display preference. It never changes Observer task or execution state.
struct LocalArchiveEntry: Codable, Identifiable {
    let id: UUID
    let source: String
    let taskRef: String
    let executionRef: String?
    let title: String

    func matches(_ task: ObservedTask, source: String) -> Bool {
        self.source == source && taskRef == task.taskRef && executionRef == task.executionRef
    }
}
