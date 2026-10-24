import AppKit
import SwiftUI
import XCTest
@testable import CLINXMonitor

/// Render real AppKit views across Observer expiry, failed reads and recovery.
/// Connection problems must never turn a running execution into a task warning.
final class ExecutionRowSnapshotTests: XCTestCase {
    @MainActor
    func testDeferredRowsKeepExecutionStateWhenConnectionSnapshotExpires() async throws {
        let source = SyntheticObserverService(scenario: .running)
        let page = try await source.tasks(active: true, offset: 0)
        let task = try XCTUnwrap(page.items.first { $0.monitorStatus == .running })
        let service = RowRecoveryService(health: try await source.health(), task: task)
        var now = Date()
        let store = MonitorStore(service: service, now: { now }, lowPowerMode: { false })
        await store.refresh()
        // Materialize the list before lazy rows render.
        let snapshot = ExecutionListView(store: store, popover: .constant(nil)).body
        now = now.addingTimeInterval(11)
        XCTAssertEqual(store.freshness, .stale, "Cross the real ten-second freshness boundary")
        let host = NSHostingView(rootView: snapshot)
        host.frame = NSRect(x: 0, y: 0, width: 420, height: 350)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = host
        defer { window.contentView = nil }
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(spinnerCount(in: host), 1,
                       "Deferred rows must agree with the CURRENT header in their captured snapshot")

        // A newly rendered list still shows the reported task state while the
        // separate runtime indicator reports the expired connection snapshot.
        host.rootView = ExecutionListView(store: store, popover: .constant(nil)).body
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(spinnerCount(in: host), 1)
        XCTAssertEqual(store.runtimeStatus, .stale)
        XCTAssertEqual(store.groupedTasks.last?.title, "Running")
        await store.refresh()
        host.rootView = ExecutionListView(store: store, popover: .constant(nil)).body
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(spinnerCount(in: host), 1)
    }

    @MainActor
    func testListPreservesTaskStateAcrossFailedReadAndRecovery() async throws {
        let source = SyntheticObserverService(scenario: .running)
        let page = try await source.tasks(active: true, offset: 0)
        let task = try XCTUnwrap(page.items.first { $0.monitorStatus == .running })
        let service = RowRecoveryService(health: try await source.health(), task: task)
        let store = MonitorStore(service: service)
        await store.refresh()
        let host = NSHostingView(rootView: ExecutionListView(store: store, popover: .constant(nil)))
        host.frame = NSRect(x: 0, y: 0, width: 420, height: 350)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = host
        defer { window.contentView = nil }

        for (unavailable, expected) in [(false, 1), (true, 1), (false, 1)] {
            await service.setUnavailable(unavailable)
            await store.refresh()
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 200_000_000)
            host.layoutSubtreeIfNeeded()
            XCTAssertEqual(store.freshness, unavailable ? .lastKnown : .current)
            XCTAssertEqual(store.runtimeStatus, unavailable ? .offline : .live)
            XCTAssertEqual(spinnerCount(in: host), expected,
                           "Observer availability must not replace a running task with a warning")
            XCTAssertEqual(store.active.first?.executionIdentity, task.executionIdentity)
            XCTAssertEqual(store.active.first?.monitorStatus, .running)
        }
        let detailReads = await service.detailReads()
        XCTAssertEqual(detailReads, 0, "List rendering must remain read-only")
    }

    @MainActor
    func testSameExecutionKeepsSpinnerAcrossConnectionFreshnessChanges() async throws {
        let service = SyntheticObserverService(scenario: .running)
        let page = try await service.tasks(active: true, offset: 0)
        let task = try XCTUnwrap(page.items.first { $0.monitorStatus == .running })
        var selections = 0
        var archives = 0
        func row(_ freshness: Freshness, selected: Bool = false) -> some View {
            ExecutionRowView(task: task, status: task.status(freshness: freshness),
                             selected: selected,
                             onSelect: { selections += 1 }, onArchive: { archives += 1 })
                .id(task.executionIdentity)
        }
        let host = NSHostingView(rootView: row(.stale))
        host.frame = NSRect(x: 0, y: 0, width: 420, height: 54)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = host
        defer { window.contentView = nil }

        for (freshness, selected, expected) in [
            (Freshness.stale, false, 1), (.current, false, 1),
            (.lastKnown, true, 1), (.current, true, 1)
        ] {
            host.rootView = row(freshness, selected: selected)
            host.layoutSubtreeIfNeeded()
            // Allow SwiftUI's representable insertion/removal to commit.
            try await Task.sleep(nanoseconds: 100_000_000)
            host.layoutSubtreeIfNeeded()
            XCTAssertEqual(spinnerCount(in: host), expected,
                           "\(freshness): the rendered glyph must follow the new snapshot")
        }
        XCTAssertEqual(task.monitorStatus, .running)
        XCTAssertEqual(selections, 0, "Rendering must not select or execute a task")
        XCTAssertEqual(archives, 0, "Rendering must not archive a task")
    }

    @MainActor
    func testInspectorKeepsRunningStateAcrossExpiryFailureAndRecovery() async throws {
        let source = SyntheticObserverService(scenario: .running)
        let page = try await source.tasks(active: true, offset: 0)
        let task = try XCTUnwrap(page.items.first { $0.monitorStatus == .running })
        let service = RowRecoveryService(health: try await source.health(), task: task)
        var now = Date()
        let store = MonitorStore(service: service, now: { now }, lowPowerMode: { false })
        await store.refresh()
        let snapshot = ExecutionHeaderView(task: task, stacked: false).body
        now = now.addingTimeInterval(11)
        XCTAssertEqual(store.freshness, .stale)
        let host = NSHostingView(rootView: snapshot)
        host.frame = NSRect(x: 0, y: 0, width: 900, height: 200)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = host
        defer { window.contentView = nil }
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(spinnerCount(in: host), 1, "Snapshot expiry must not replace execution state")
        host.rootView = ExecutionHeaderView(task: task, stacked: false).body
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(spinnerCount(in: host), 1, "Snapshot expiry must not overwrite execution state")

        // Also exercise the real observing parent, including failed reads and
        // recovery while all task fields stay unchanged.
        await store.select(task.taskRef, executionRef: task.executionRef)
        let inspector = NSHostingView(rootView: InspectorView(store: store, stacked: false))
        inspector.frame = NSRect(x: 0, y: 0, width: 900, height: 600)
        window.contentView = inspector
        for (unavailable, expected) in [(false, 1), (true, 1), (false, 1)] {
            await service.setUnavailable(unavailable)
            await store.refresh()
            inspector.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 200_000_000)
            inspector.layoutSubtreeIfNeeded()
            XCTAssertEqual(spinnerCount(in: inspector), expected)
            XCTAssertEqual(store.selected?.executionIdentity, task.executionIdentity)
            XCTAssertEqual(store.selected?.monitorStatus, .running)
        }
    }

    @MainActor
    func testConnectionFreshnessUsesPollingCadenceAndKeepsFailuresInRuntimeStatus() async throws {
        let source = SyntheticObserverService(scenario: .running)
        let page = try await source.tasks(active: true, offset: 0)
        let task = try XCTUnwrap(page.items.first { $0.monitorStatus == .running })
        let service = RowRecoveryService(health: try await source.health(), task: task)
        var now = Date()
        var lowPower = true
        let store = MonitorStore(service: service, now: { now }, lowPowerMode: { lowPower })
        await store.refresh()
        now = now.addingTimeInterval(15)
        XCTAssertEqual(store.connection, .connected, "A healthy scheduled low-power wait must not warn")
        XCTAssertEqual(store.status(of: task), .running)
        now = now.addingTimeInterval(31)
        XCTAssertEqual(store.connection, .stale, "Low-power snapshots still have a finite expiry")
        await store.refresh()
        XCTAssertEqual(store.connection, .connected)
        await service.setUnavailable(true)
        await store.refresh()
        XCTAssertEqual(store.freshness, .lastKnown, "Connection failure remains visible in runtime status")
        XCTAssertEqual(store.runtimeStatus, .offline)
        XCTAssertEqual(store.status(of: task), .running)
        await service.setUnavailable(false)
        lowPower = false
        await store.refresh()
        now = now.addingTimeInterval(9)
        XCTAssertEqual(store.connection, .connected)
        now = now.addingTimeInterval(2)
        XCTAssertEqual(store.connection, .stale, "Normal active polling retains its ten-second limit")
        await store.refresh()
        XCTAssertEqual(store.connection, .connected)
        XCTAssertEqual(MonitorPollingPolicy.interval(hasActiveTasks: false, lowPowerMode: false), 15)
        XCTAssertEqual(MonitorPollingPolicy.freshnessLimit(hasActiveTasks: false, lowPowerMode: false), 45)
    }

    @MainActor
    private func spinnerCount(in view: NSView) -> Int {
        (view is LayerSpinner.SpinnerView ? 1 : 0)
            + view.subviews.reduce(0) { $0 + spinnerCount(in: $1) }
    }
}

private actor RowRecoveryService: ObserverServing {
    let healthValue: ObserverHealth
    let taskValue: ObservedTask
    private var unavailable = false
    private var reads = 0
    init(health: ObserverHealth, task: ObservedTask) { healthValue = health; taskValue = task }
    func setUnavailable(_ value: Bool) { unavailable = value }
    func detailReads() -> Int { reads }
    func health() async throws -> ObserverHealth {
        if unavailable { throw MonitorError.server(503) }
        return healthValue
    }
    func tasks(active: Bool, offset: Int) async throws -> TaskPage {
        TaskPage(schemaVersion: "1", observedAt: taskValue.timestamps.observedAt,
                 items: active ? [taskValue] : [], nextOffset: nil, hasMore: false)
    }
    func task(_ ref: String) async throws -> ObservedTask { reads += 1; return taskValue }
    func events(_ ref: String, after: String?) async throws -> EventPage { reads += 1; return taskValue.recentEvents }
}
