import XCTest
import Combine
@testable import CLINXMonitor

private func message(_ id: String, _ order: Int64, _ revision: Int64, _ text: String) -> ActivityMessage {
    ActivityMessage(id: id, ordinal: order, revision: revision, timestampMs: order * 1000,
                    kind: "feedback", text: text, truncated: false)
}

private func page(_ messages: [ActivityMessage], execution: String = "exec_test",
                  cursor: String = "next", older: String? = nil) -> ActivityPage {
    ActivityPage(schemaVersion: "1", taskRef: "task_test", executionRef: execution,
                 observedAt: "2026-10-02T04:00:00Z", source: "CODEX_NATIVE_HISTORY", availability: "AVAILABLE",
                 reason: nil, items: messages, nextCursor: cursor, olderCursor: older, hasMore: older != nil)
}

@MainActor
final class ActivityTests: XCTestCase {
    func testUnchangedBackgroundPollOnlyPublishesSyncTime() async throws {
        let store = ActivityStore(taskRef: "task_test", executionRef: "exec_test",
                                  service: EmptyDeltaActivityService())
        try store.accept(page([message("a", 1, 1, "existing")]), initial: true)
        var transcriptUpdates = 0
        var syncUpdates = 0
        let transcript = store.objectWillChange.sink { transcriptUpdates += 1 }
        let sync = store.syncStatus.objectWillChange.sink { syncUpdates += 1 }
        await store.refresh()
        XCTAssertEqual(transcriptUpdates, 0, "An empty background delta must not invalidate the scroll tree")
        XCTAssertEqual(syncUpdates, 1, "The sync label must still refresh at the existing frequency")
        XCTAssertEqual(store.messages.first?.text, "existing")
        XCTAssertEqual(store.cursor, "delta")
        // Replayed messages must likewise leave the transcript unchanged.
        try store.accept(page([message("a", 1, 1, "existing")]))
        XCTAssertEqual(transcriptUpdates, 0)
        withExtendedLifetime((transcript, sync)) {}
    }

    func testDecodedMarkdownIsPreparedWithoutChangingWirePayload() throws {
        let original = message("a", 1, 1, "**Bold** and `code`\n下一行")
        let data = try JSONEncoder().encode(original)
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNil(wire["formattedText"])
        XCTAssertEqual(wire["text"] as? String, original.text)
        let decoded = try JSONDecoder().decode(ActivityMessage.self, from: data)
        XCTAssertEqual(decoded, original)
        XCTAssertEqual(String(decoded.formattedText.characters), "Bold and code\n下一行")
        XCTAssertTrue(decoded.formattedText.runs.contains { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true })
        XCTAssertTrue(decoded.formattedText.runs.contains { $0.inlinePresentationIntent?.contains(.code) == true })
    }

    func testMergesRevisionsInCreationOrderAndIgnoresReplay() throws {
        let store = ActivityStore(taskRef: "task_test", executionRef: "exec_test", service: nil)
        try store.accept(page([message("a", 1, 1, "first"), message("b", 2, 2, "second")]), initial: true)
        try store.accept(page([message("a", 1, 4, "updated"), message("c", 3, 3, "third")]))
        try store.accept(page([message("a", 1, 1, "old")]))
        XCTAssertEqual(store.messages.map(\.text), ["updated", "second", "third"])
        XCTAssertEqual(store.messages.map(\.id), ["a", "b", "c"])
    }

    func testOlderPageDoesNotAdvanceLiveCursorOrOverwriteNewerRevision() throws {
        let store = ActivityStore(taskRef: "task_test", executionRef: "exec_test", service: nil)
        try store.accept(page([message("b", 2, 5, "updated")], cursor: "live5", older: "old2"), initial: true)
        try store.accept(page([message("a", 1, 1, "first"), message("b", 2, 2, "stale")],
                              cursor: "unrelated", older: nil), older: true)
        XCTAssertEqual(store.cursor, "live5")
        XCTAssertNil(store.olderCursor)
        XCTAssertEqual(store.messages.map(\.text), ["first", "updated"])
    }

    func testRejectsWrongExecutionWithoutContaminatingCurrentFeed() throws {
        let store = ActivityStore(taskRef: "task_test", executionRef: "exec_test", service: nil)
        try store.accept(page([message("a", 1, 1, "visible")]))
        XCTAssertThrowsError(try store.accept(page([message("x", 2, 2, "wrong task")], execution: "exec_other")))
        XCTAssertEqual(store.messages.map(\.text), ["visible"])
    }

    func testLateResponseIsDiscardedWhenTabCloses() async {
        let service = DelayedActivityService()
        let store = ActivityStore(taskRef: "task_test", executionRef: "exec_test", service: service)
        let request = Task { await store.refresh() }
        while !(await service.waiting) { await Task.yield() }
        store.stop()
        await service.finish()
        await request.value
        XCTAssertTrue(store.messages.isEmpty)
        XCTAssertNil(store.cursor)
    }

    func testScrollObserverPublishesOnlyBottomStateTransitions() {
        var transition = ActivityScrollTransition()

        XCTAssertEqual(transition.update(false), false)
        XCTAssertNil(transition.update(false))
        XCTAssertNil(transition.update(false))
        XCTAssertEqual(transition.update(true), true)
        XCTAssertNil(transition.update(true))
        XCTAssertEqual(transition.update(false), false)
    }

    func testTerminalActivityRunStopsAfterResultTail() async throws {
        let service = TerminalActivityService()
        let store = ActivityStore(taskRef: "task_test", executionRef: "exec_test",
                                  service: service, terminal: true)

        await store.run()

        XCTAssertEqual(await service.requestCount, 1)
        XCTAssertEqual(store.messages.map(\.kind), ["result"])
    }
}

private struct EmptyDeltaActivityService: ActivityServing {
    func activity(_ ref: String, execution: String, after: String?, before: String?) async throws -> ActivityPage {
        page([], cursor: "delta")
    }
}

private actor DelayedActivityService: ActivityServing {
    private var continuation: CheckedContinuation<ActivityPage, Never>?
    var waiting: Bool { continuation != nil }
    func activity(_ ref: String, execution: String, after: String?, before: String?) async throws -> ActivityPage {
        await withCheckedContinuation { continuation = $0 }
    }
    func finish() {
        continuation?.resume(returning: page([message("a", 1, 1, "late")]))
        continuation = nil
    }
}

private actor TerminalActivityService: ActivityServing {
    private(set) var requestCount = 0

    func activity(_ ref: String, execution: String, after: String?, before: String?) async throws -> ActivityPage {
        requestCount += 1
        return page([message("result", 1, 1, "最终报告" )])
    }
}
