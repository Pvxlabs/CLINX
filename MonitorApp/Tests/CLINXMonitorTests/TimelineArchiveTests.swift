import XCTest
@testable import CLINXMonitor

final class TimelineArchiveTests: XCTestCase {
    @MainActor
    func testSidebarCountsMatchEveryViewAfterFilteringArchivingAndRestoring() async throws {
        let suite = "CLINXCountTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = MonitorStore(service: SyntheticObserverService(scenario: .blocked), defaults: defaults)
        await store.refresh()

        func assertCounts(file: StaticString = #filePath, line: UInt = #line) {
            for view in MonitorView.allCases {
                store.view = view
                XCTAssertEqual(store.counts[view], store.visibleTasks.count, file: file, line: line)
            }
        }

        assertCounts()
        store.timeWindow = .hour
        assertCounts()
        let task = try XCTUnwrap(store.allTasks.first { store.status(of: $0).isAttention })
        store.hostFilter = task.hostText
        assertCounts()
        store.projectFilter = task.projectText
        assertCounts()
        store.searchText = task.taskRef
        assertCounts()
        XCTAssertEqual(store.counts[.active], 1)
        store.archiveLocally(task)
        assertCounts()
        XCTAssertEqual(store.counts[.active], 0)
        XCTAssertEqual(store.hostOptions.first { $0.name == task.hostText }?.count ?? 0,
                       store.allTasks.filter { $0.hostText == task.hostText && !store.isLocallyArchived($0) }.count)
        XCTAssertEqual(store.projectOptions.first { $0.name == task.projectText }?.count ?? 0,
                       store.allTasks.filter { $0.projectText == task.projectText && !store.isLocallyArchived($0) }.count)
        await store.refresh()
        assertCounts()
        XCTAssertEqual(store.counts[.active], 0)
        store.restoreLocalArchive(try XCTUnwrap(store.currentArchives.first).id)
        assertCounts()
        XCTAssertEqual(store.counts[.active], 1)
        store.searchText = "no-matching-execution"
        assertCounts()
        XCTAssertTrue(store.counts.values.allSatisfy { $0 == 0 })
    }

    @MainActor
    func testLocalArchivePersistsAndRestoresWithoutChangingCanonicalTasks() async throws {
        let suite = "CLINXArchiveTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("https://p620.example.test", forKey: "monitor.endpoint")
        let service = SyntheticObserverService(scenario: .blocked)
        let store = MonitorStore(service: service, defaults: defaults)
        await store.refresh()
        let task = try XCTUnwrap(store.visibleTasks.first { store.status(of: $0).isAttention })
        let count = store.counts[.active]!
        await store.select(task.taskRef)
        store.archiveLocally(task)
        XCTAssertNil(store.selectedRef)
        XCTAssertNil(store.selected)
        XCTAssertFalse(store.visibleTasks.contains { $0.taskRef == task.taskRef })
        XCTAssertTrue(store.allTasks.contains { $0.taskRef == task.taskRef && $0.state == task.state })
        XCTAssertEqual(store.counts[.active], count - 1)
        await store.refresh()
        XCTAssertTrue(store.isLocallyArchived(task))

        let relaunched = MonitorStore(service: service, defaults: defaults)
        await relaunched.refresh()
        XCTAssertFalse(relaunched.visibleTasks.contains { $0.taskRef == task.taskRef })
        let entry = try XCTUnwrap(relaunched.currentArchives.first)
        relaunched.restoreLocalArchive(entry.id)
        XCTAssertTrue(relaunched.visibleTasks.contains { $0.taskRef == task.taskRef })
        XCTAssertTrue(MonitorStore(service: service, defaults: defaults).currentArchives.isEmpty)
    }

    @MainActor
    func testArchiveIsScopedToSourceAndExecutionAndRejectsRunning() async throws {
        let suite = "CLINXArchiveTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("https://p620.example.test", forKey: "monitor.endpoint")
        let service = SyntheticObserverService(scenario: .blocked)
        let store = MonitorStore(service: service, defaults: defaults)
        await store.refresh()
        let running = try XCTUnwrap(store.allTasks.first { store.status(of: $0) == .running })
        store.archiveLocally(running)
        XCTAssertTrue(store.currentArchives.isEmpty)
        let blocked = try XCTUnwrap(store.allTasks.first { store.status(of: $0).isAttention })
        store.archiveLocally(blocked)
        store.archiveLocally(blocked)
        XCTAssertEqual(store.currentArchives.count, 1)
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(blocked)) as? [String: Any])
        json["executionRef"] = "exec_new_run"
        let newRun = try JSONDecoder().decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: json))
        XCTAssertFalse(store.isLocallyArchived(newRun))
        defaults.set("https://another.example.test", forKey: "monitor.endpoint")
        let other = MonitorStore(service: service, defaults: defaults)
        XCTAssertFalse(other.isLocallyArchived(blocked))
        XCTAssertTrue(other.currentArchives.isEmpty)
    }

    func testLatestTenUsesFullDatesAcrossMidnightAndExpandsAll() throws {
        let task = try XCTUnwrap(SyntheticTasks.all().first)
        let events = (0..<12).map { index in
            let stamp = index == 0 ? "2026-10-01T23:59:59Z" : String(format: "2026-10-02T00:00:%02dZ", index)
            return ObserverEvent(cursor: "\(index)", eventRef: "event_\(index)", kind: "V1ExecutionProgressObserved",
                                 executionRef: task.executionRef, occurredAt: stamp, recordedAt: stamp)
        }
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(task)) as? [String: Any])
        json["hostOperations"] = []
        let noOperations = try JSONDecoder().decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: json))
        let timeline = noOperations.withEvents(Array(events.reversed()), coverage: "COMPLETE", hasMore: false).timeline
        XCTAssertEqual(timeline.map(\.id), events.map(\.eventRef))
        XCTAssertEqual(TimelineView.visibleItems(timeline, expanded: false).map(\.id), Array(events.suffix(10)).map(\.eventRef))
        XCTAssertEqual(TimelineView.visibleItems(timeline, expanded: true).count, 12)
        XCTAssertTrue(TimelineView.visibleItems([], expanded: false).isEmpty)
        XCTAssertEqual(TimelineView.visibleItems(Array(timeline.prefix(3)), expanded: false).count, 3)
    }
}
