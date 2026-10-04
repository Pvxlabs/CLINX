import XCTest
@testable import CLINXMonitor

private func networkFixture(_ key: String = "detail", id: String? = nil,
                            turn: String? = nil) throws -> ObservationDetail {
    let url = try XCTUnwrap(Bundle.module.url(forResource: "network-observations", withExtension: "json"))
    let all = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    var raw = try XCTUnwrap(all[key] as? [String: Any])
    var item = try XCTUnwrap(raw["item"] as? [String: Any])
    if let id { item["observation_id"] = id }
    if let turn {
        var value = try XCTUnwrap(item["turn"] as? [String: Any])
        value["turn_id"] = turn
        item["turn"] = value
        raw["turns"] = [value]
    }
    raw["item"] = item
    raw["next_cursor"] = "older"
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(ObservationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
}

final class NetworkObservationModelTests: XCTestCase {
    func testExternalCompletedIsVisibleWithoutCanonicalReferencesOrPass() throws {
        let item = try networkFixture().item
        XCTAssertNil(item.taskRef)
        XCTAssertNil(item.turn.executionRef)
        XCTAssertNil(item.turn.businessResult)
        XCTAssertEqual(item.id, item.observationId)
        XCTAssertFalse(item.isRunning)
        XCTAssertFalse(item.control.enabled)
        XCTAssertLessThanOrEqual(item.shortTitle.count, 100)
        XCTAssertEqual(item.bindingEvidence, "THREAD_UNBOUND")
    }
    func testActivityPreservesEventAndRoundIdentities() throws {
        let data = Data(#"""
        {"schema_version":"clinx-observation-v1","observation_id":"obs_fixture",
         "items":[{"event_id":"stream:2","turn_id":"turn-a","execution_ref":null,
                   "source_seq":2,"recorded_at":100.5,"native_state":"RUNNING",
                   "business_result":null,"kind":"progress","text":"公开进度","artifacts":[]}],
         "coverage":"RECEIVED_EVENTS_ONLY","next_cursor":"older","has_more":true}
        """#.utf8)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let page = try decoder.decode(ObservationActivityPage.self, from: data)
        XCTAssertEqual(page.items.first?.id, "stream:2")
        XCTAssertEqual(page.items.first?.turnId, "turn-a")
        XCTAssertNil(page.items.first?.executionRef)
        XCTAssertEqual(page.nextCursor, "older")
    }

    func testMissingResultMarkerStopsSpinnerAndKeepsBusinessUncertainty() throws {
        let item = try networkFixture("missing_result").item
        XCTAssertFalse(item.isRunning)
        XCTAssertEqual(item.statusLabel, "执行结束 · 结果待处理")
        XCTAssertNil(item.turn.businessResult)
    }
}

private actor NetworkGate: NetworkObservationServing {
    var waiting: [String: CheckedContinuation<ObservationDetail, Error>] = [:]
    let first: ObservationDetail
    init(_ first: ObservationDetail) { self.first = first }
    func observations(filters: ObservationFilters, cursor: String?) async throws -> ObservationPage {
        ObservationPage(schemaVersion: "clinx-observation-v1", items: [first.item],
                        nextCursor: nil, hasMore: false, coverage: [:], retentionEvicted: 0)
    }
    func observation(_ id: String, cursor: String?) async throws -> ObservationDetail {
        try await withCheckedThrowingContinuation { waiting[id] = $0 }
    }
    func observationContext(_ id: String, cursor: String?) async throws -> ObservationContext {
        ObservationContext(schemaVersion: "clinx-observation-v1", contextStatus: "UNCACHED_CONTENT_UNAVAILABLE",
                           lastUserIntent: nil, lastCodexResult: nil, nextCursor: nil)
    }
    func pending(_ id: String) -> Bool { waiting[id] != nil }
    func finish(_ id: String, detail: ObservationDetail) { waiting.removeValue(forKey: id)?.resume(returning: detail) }
}

@MainActor
final class NetworkObservationStoreTests: XCTestCase {
    func testDelayedSelectionCannotContaminateAnotherThread() async throws {
        let first = try networkFixture(id: "obs_" + String(repeating: "a", count: 40), turn: "turn-a")
        let second = try networkFixture(id: "obs_" + String(repeating: "b", count: 40), turn: "turn-b")
        let service = NetworkGate(first)
        let store = NetworkObservationStore(client: service)
        let a = Task { await store.select(first.item.id) }
        while !(await service.pending(first.item.id)) { await Task.yield() }
        let b = Task { await store.select(second.item.id) }
        while !(await service.pending(second.item.id)) { await Task.yield() }
        await service.finish(second.item.id, detail: second)
        await b.value
        await service.finish(first.item.id, detail: first)
        await a.value
        XCTAssertEqual(store.detail?.item.id, second.item.id)
        XCTAssertEqual(store.turns.map(\.id), ["turn-b"])
    }

    func testEndpointChangeInvalidatesInFlightDetail() async throws {
        let detail = try networkFixture()
        let service = NetworkGate(detail)
        let store = NetworkObservationStore(client: service)
        let pending = Task { await store.select(detail.item.id) }
        while !(await service.pending(detail.item.id)) { await Task.yield() }
        store.configure(endpoint: "http://invalid", account: "fixture")
        await service.finish(detail.item.id, detail: detail)
        await pending.value
        XCTAssertNil(store.detail)
        XCTAssertTrue(store.turns.isEmpty)
    }
}
