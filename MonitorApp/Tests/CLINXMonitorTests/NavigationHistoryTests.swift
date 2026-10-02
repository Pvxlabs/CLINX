import XCTest
@testable import CLINXMonitor

final class NavigationHistoryTests: XCTestCase {
    @MainActor
    func testBackForwardRestoresContextAndNewVisitReplacesForwardBranch() async throws {
        let suite = "CLINXNavigationTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = MonitorStore(service: SyntheticObserverService(scenario: .blocked), defaults: defaults)
        await store.refresh()
        let tasks = Array(store.allTasks.prefix(3))
        XCTAssertEqual(tasks.count, 3)
        XCTAssertFalse(store.canGoBack)
        XCTAssertFalse(store.canGoForward)
        store.view = .active
        store.hostFilter = tasks[0].hostText
        await store.select(tasks[0].taskRef)
        await store.select(tasks[0].taskRef)
        XCTAssertEqual(store.navigationHistory.count, 1)
        store.view = .recent
        store.hostFilter = nil
        store.timeWindow = .week
        await store.select(tasks[1].taskRef)
        store.goBack()
        XCTAssertEqual(store.selectedRef, tasks[0].taskRef)
        XCTAssertEqual(store.view, .active)
        XCTAssertEqual(store.hostFilter, tasks[0].hostText)
        XCTAssertEqual(store.timeWindow, .day)
        XCTAssertTrue(store.canGoForward)
        XCTAssertFalse(store.canGoBack)
        store.goForward()
        XCTAssertEqual(store.selectedRef, tasks[1].taskRef)
        XCTAssertEqual(store.view, .recent)
        XCTAssertNil(store.hostFilter)
        XCTAssertEqual(store.timeWindow, .week)
        store.goBack()
        await store.select(tasks[2].taskRef)
        XCTAssertEqual(store.navigationHistory.map(\.taskRef), [tasks[0].taskRef, tasks[2].taskRef])
        XCTAssertFalse(store.canGoForward)
        store.openHistory(at: 0)
        XCTAssertEqual(store.selectedRef, tasks[0].taskRef)
        XCTAssertEqual(store.navigationHistory.count, 2)
    }

    @MainActor
    func testHistoryIsBoundedAndArchiveAndSourceSwitchRemoveVisits() async throws {
        let suite = "CLINXNavigationTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = MonitorStore(service: SyntheticObserverService(scenario: .blocked), defaults: defaults)
        await store.refresh()
        let blocked = try XCTUnwrap(store.allTasks.first { store.status(of: $0).isAttention })
        let other = try XCTUnwrap(store.allTasks.first { $0.taskRef != blocked.taskRef })
        for index in 0..<52 { await store.select(index.isMultiple(of: 2) ? blocked.taskRef : other.taskRef) }
        XCTAssertEqual(store.navigationHistory.count, 50)
        XCTAssertEqual(store.navigationIndex, 49)
        store.archiveLocally(blocked)
        XCTAssertFalse(store.navigationHistory.contains { $0.taskRef == blocked.taskRef })
        XCTAssertEqual(store.navigationIndex, 24)
        XCTAssertEqual(store.selectedRef, other.taskRef)
        store.useSynthetic(.empty)
        store.stop()
        XCTAssertTrue(store.navigationHistory.isEmpty)
        XCTAssertNil(store.navigationIndex)
        XCTAssertFalse(store.canGoBack)
        XCTAssertFalse(store.canGoForward)
    }
}
