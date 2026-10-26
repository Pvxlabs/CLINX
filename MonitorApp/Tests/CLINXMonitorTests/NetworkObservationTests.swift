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
    func testModelComesFromTheSelectedNativeTurn() throws {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(try networkFixture().item)) as? [String: Any])
        var turn = try XCTUnwrap(raw["turn"] as? [String: Any])
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        for model in ["gpt-6-astra", "gpt-6.1-sol"] {
            turn["model"] = model
            raw["turn"] = turn
            let item = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
            XCTAssertEqual(NetworkObservationAdapter.project([item], canonical: []).first?.modelText, model)
        }
        turn.removeValue(forKey: "model")
        raw["turn"] = turn
        let olderObserverItem = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertEqual(NetworkObservationAdapter.project([olderObserverItem], canonical: []).first?.modelText, "UNKNOWN")
    }

    func testRepeatedSnapshotsDoNotRepeatSessionTextAcrossTurns() {
        func entry(_ id: String, _ turn: String, _ text: String) -> ObservationActivityEntry {
            ObservationActivityEntry(eventId: id, turnId: turn, executionRef: nil,
                sourceSeq: 1, recordedAt: 100, nativeState: "COMPLETED", businessResult: nil,
                kind: "result", text: text, artifacts: [])
        }
        let oldestFirst = [entry("a1", "turn-a", "first"), entry("b1", "turn-b", "second"),
                           entry("a2", "turn-a", "first"), entry("b2", "turn-b", "second"),
                           entry("a3", "turn-a", "changed"), entry("c1", "turn-c", "first")]
        let displayed = ObservationHistoryRenderer.distinctUpdates(Array(oldestFirst.reversed()))
        XCTAssertEqual(displayed.map(\.id), ["a1", "b1", "a3", "c1"])
        XCTAssertEqual(displayed.map(\.text), ["first", "second", "changed", "first"])
    }

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
        XCTAssertEqual(item.statusLabel, item.turn.nativeState)
        XCTAssertNil(item.turn.businessResult)
    }
}

private func projectedObservation(node: String, host: String, thread: String, state: String,
                                  taskRef: String? = nil, liveness: String = "NOT_PROVEN", executionRef: String? = nil) throws -> NetworkObservation {
    let fixture = try networkFixture().item
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(fixture)) as? [String: Any])
    raw["node_id"] = node
    raw["device_name"] = host
    raw["native_thread_id"] = thread
    raw["observation_id"] = "obs_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased() + "00000000"
    raw["task_ref"] = taskRef.map { $0 as Any } ?? NSNull()
    raw["binding_evidence"] = taskRef == nil ? "THREAD_UNBOUND" : "CANONICAL_ROUTE"
    raw["received_at"] = Date().timeIntervalSince1970
    raw["source_updated_at"] = ISO8601DateFormatter().string(from: Date())
    raw["liveness"] = liveness
    raw["liveness_valid_until"] = liveness == "PROVEN" ? Date().timeIntervalSince1970 + 25 : NSNull()
    var turn = try XCTUnwrap(raw["turn"] as? [String: Any])
    turn["native_state"] = state
    turn["execution_ref"] = executionRef.map { $0 as Any } ?? NSNull()
    raw["turn"] = turn
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
}

private func canonicalFixture() throws -> ObservedTask {
    let url = try XCTUnwrap(Bundle.module.url(forResource: "api-examples", withExtension: "json"))
    let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let examples = try XCTUnwrap(root["examples"] as? [[String: Any]])
    let entry = try XCTUnwrap(examples.first { $0["name"] as? String == "pass_detail" })
    let raw = try XCTUnwrap(entry["response"] as? [String: Any])
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: raw))
}

private actor UnifiedProjectionService: ObserverServing, NetworkObservationServing {
    let canonical: [ObservedTask]
    let native: [NetworkObservation]
    let paginated: Bool
    private var observationFailure: MonitorError?
    private(set) var taskCalls = 0
    private(set) var eventCalls = 0

    init(canonical: [ObservedTask] = [], native: [NetworkObservation], paginated: Bool = false) {
        self.canonical = canonical
        self.native = native
        self.paginated = paginated
    }
    func failObservations(_ error: MonitorError?) { observationFailure = error }
    func health() async throws -> ObserverHealth {
        ObserverHealth(schemaVersion: "1", status: "OK", readOnly: true,
                       observedAt: ISO8601DateFormatter().string(from: Date()),
                       authorityLiveness: "UNKNOWN", authMode: "TEST", eventsTransport: "POLL")
    }
    func tasks(active: Bool, offset: Int) async throws -> TaskPage {
        TaskPage(schemaVersion: "1", observedAt: ISO8601DateFormatter().string(from: Date()),
                 items: active ? canonical : [], nextOffset: nil, hasMore: false)
    }
    func task(_ ref: String) async throws -> ObservedTask {
        taskCalls += 1
        return canonical[0]
    }
    func events(_ ref: String, after: String?) async throws -> EventPage {
        eventCalls += 1
        return canonical[0].recentEvents
    }
    func observations(filters: ObservationFilters, cursor: String?) async throws -> ObservationPage {
        if let observationFailure { throw observationFailure }
        let candidates = native.filter { (filters.state.isEmpty || $0.turn.nativeState == filters.state) && (filters.nativeThreadId.isEmpty || $0.nativeThreadId == filters.nativeThreadId) }
        let offset = Int(cursor ?? "0") ?? 0
        let page = paginated ? Array(candidates.dropFirst(offset).prefix(50)) : candidates
        let more = paginated && offset + page.count < candidates.count
        return ObservationPage(schemaVersion: "clinx-observation-v1", items: page,
                        nextCursor: more ? String(offset + page.count) : nil, hasMore: more,
                        coverage: [:], retentionEvicted: 0,
                        sources: paginated ? [ObservationSource(nodeId: "p620", displayName: "P620",
                            state: "ONLINE", coverage: "RECEIVED", ackSeq: 1, gap: nil)] : nil)
    }
    func observation(_ id: String, cursor: String?) async throws -> ObservationDetail {
        throw MonitorError.invalidResponse
    }
    func observationContext(_ id: String, cursor: String?) async throws -> ObservationContext {
        throw MonitorError.invalidResponse
    }
}

@MainActor
final class UnifiedMonitorProjectionTests: XCTestCase {
    func testObservationFailureKeepsLastKnownRowsAndRecoversWithoutFalseCurrent() async throws {
        let native = try projectedObservation(node: "p620", host: "P620", thread: "active-hdl",
            state: "RUNNING", liveness: "PROVEN")
        let canonical = try canonicalFixture()
        let service = UnifiedProjectionService(canonical: [canonical], native: [native])
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()
        let ref = try XCTUnwrap(store.allTasks.first { $0.observationId == native.observationId }).taskRef
        let activeCount = store.active.count
        await store.select(ref)
        let successfulFetch = store.lastSuccessfulFetch
        XCTAssertEqual(store.freshness, .current)

        await service.failObservations(.server(400))
        await store.refresh()
        XCTAssertTrue(store.allTasks.contains { $0.observationId == native.observationId })
        XCTAssertTrue(store.allTasks.contains { $0.taskRef == canonical.taskRef })
        XCTAssertEqual(store.selected?.observationId, native.observationId)
        XCTAssertEqual(store.freshness, .lastKnown)
        XCTAssertEqual(store.lastSuccessfulFetch, successfulFetch)
        XCTAssertNotNil(store.errorCategory)
        if let row = store.allTasks.first(where: { $0.observationId == native.observationId }) {
            XCTAssertEqual(store.status(of: row), .running)
            XCTAssertEqual(store.runtimeStatus, .offline)
            XCTAssertTrue(store.visibleTasks.contains { $0.observationId == native.observationId })
        }

        await service.failObservations(nil)
        await store.refresh()
        XCTAssertEqual(store.connection, .connected)
        XCTAssertEqual(store.freshness, .current)
        XCTAssertNil(store.errorCategory)
        XCTAssertEqual(store.active.count, activeCount)
        XCTAssertEqual(store.selected?.observationId, native.observationId)
    }

    func testInitialObservationFailureKeepsCanonicalRowsAndReportsUnavailable() async throws {
        let canonical = try canonicalFixture()
        let service = UnifiedProjectionService(canonical: [canonical], native: [])
        await service.failObservations(.server(503))
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()
        XCTAssertEqual(store.allTasks.map(\.taskRef), [canonical.taskRef])
        XCTAssertNotEqual(store.freshness, .current)
        XCTAssertNotNil(store.errorCategory)
        XCTAssertNil(store.lastSuccessfulFetch)
    }

    func testRustTerminalProjectionPreservesExactExecutionWithoutBusinessPass() throws {
        let fixture = try projectedObservation(node: "p620", host: "P620", thread: "rust-thread",
            state: "UNKNOWN", taskRef: "task_rust", executionRef: "exec_rust")
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(fixture)) as? [String: Any])
        var turn = try XCTUnwrap(raw["turn"] as? [String: Any])
        turn["execution_state"] = "SUCCEEDED"
        raw["turn"] = turn
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let item = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
        let row = try XCTUnwrap(NetworkObservationAdapter.project([item], canonical: []).first)
        XCTAssertEqual(row.executionRef, "exec_rust")
        XCTAssertEqual(row.state, "UNKNOWN", "A CLINX result must not invent a Codex terminal state")
        XCTAssertEqual(row.executionState, "SUCCEEDED")
        XCTAssertFalse(row.resultPassed)
        XCTAssertFalse(item.turn.isTerminal)
        XCTAssertEqual(item.statusLabel, "UNKNOWN")
    }

    func testNativeProjectionAndOriginalHostCounts() async throws {
        let items = try [
            projectedObservation(node: "p620", host: "P620", thread: "a", state: "RUNNING", liveness: "PROVEN"),
            projectedObservation(node: "air", host: "Air", thread: "b", state: "FAILED"),
            projectedObservation(node: "imac", host: "iMac", thread: "c", state: "COMPLETED"),
            projectedObservation(node: "air", host: "Air", thread: "d", state: "UNKNOWN")
        ]
        let service = UnifiedProjectionService(native: items)
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()

        XCTAssertEqual(store.active.count, 1)
        XCTAssertEqual(store.recent.count, 3)
        XCTAssertEqual(store.allTasks.count, 4)
        XCTAssertEqual(store.counts[.active], 1)
        XCTAssertEqual(store.counts[.failed], 1)
        XCTAssertEqual(store.counts[.completed], 1)
        XCTAssertEqual(store.hostOptions.map { ($0.name, $0.count) }.count, 3)
        XCTAssertEqual(store.hostOptions.first { $0.name == "air" }?.count, 2)
        XCTAssertEqual(store.hostOptions.first { $0.name == "p620" }?.count, 1)
        XCTAssertEqual(store.hostOptions.first { $0.name == "imac" }?.count, 1)
        let unknown = try XCTUnwrap(store.recent.first { $0.stage == "UNKNOWN" })
        XCTAssertEqual(unknown.monitorStatus, .unknown)
        XCTAssertFalse(store.active.contains { $0.taskRef == unknown.taskRef })
        store.view = .completed
        XCTAssertFalse(store.visibleTasks.contains { $0.taskRef == unknown.taskRef })

        await store.select(try XCTUnwrap(store.active.first).taskRef)
        XCTAssertEqual(store.selected?.hostText, "p620")
        XCTAssertEqual(store.selected?.observationId, items[0].observationId)
        XCTAssertNil(store.selected?.executionRef, "A native content route must not fabricate a CLINX execution")
        let selected = try XCTUnwrap(store.selected)
        let restored = try JSONDecoder().decode(ObservedTask.self, from: JSONEncoder().encode(selected))
        XCTAssertEqual(restored.observationId, items[0].observationId)
        let nativeTaskCalls = await service.taskCalls
        let nativeEventCalls = await service.eventCalls
        XCTAssertEqual(nativeTaskCalls, 0)
        XCTAssertEqual(nativeEventCalls, 0)
    }

    func testCanonicalBindingDeduplicatesButCodexOwnsTheDisplayedTurnState() async throws {
        let canonical = try canonicalFixture()
        let bound = try projectedObservation(node: "air", host: "Air", thread: "same", state: "RUNNING",
                                             taskRef: canonical.taskRef, executionRef: canonical.executionRef)
        let unbound = try projectedObservation(node: "air", host: "Air", thread: "same", state: "RUNNING")
        let service = UnifiedProjectionService(canonical: [canonical], native: [bound, unbound])
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()

        XCTAssertEqual(store.allTasks.count, 1)
        XCTAssertEqual(store.allTasks.first?.taskRef, canonical.taskRef)
        XCTAssertFalse(store.allTasks.contains { NetworkObservationAdapter.isPresentationRef($0.taskRef) })
        await store.select(canonical.taskRef)
        XCTAssertEqual(store.selected?.observationId, bound.observationId)
        XCTAssertEqual(store.selected?.monitorStatus, .running)
        let canonicalTaskCalls = await service.taskCalls
        XCTAssertEqual(canonicalTaskCalls, 0)
    }

    func testRegisteredNativeUsesCanonicalTaskIDAndObservationHistory() async throws {
        let item = try projectedObservation(node: "p620", host: "P620", thread: "native-task",
            state: "COMPLETED", taskRef: "task_canonical_native")
        let service = UnifiedProjectionService(native: [item])
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()
        XCTAssertEqual(store.allTasks.map(\.taskRef), ["task_canonical_native"])
        XCTAssertNil(store.allTasks.first?.executionRef)
        XCTAssertEqual(store.allTasks.first?.nativeConversation?.threadId, "native-task")
        await store.select("task_canonical_native")
        XCTAssertEqual(store.selected?.observationId, item.observationId)
        let calls = await service.taskCalls
        XCTAssertEqual(calls, 0, "Native history stays on the observation route after canonical registration")
    }

    func testCodexRunningStaysRunningWhenCollectionProofIsUnavailable() throws {
        let stale = try projectedObservation(node: "p620", host: "P620", thread: "stale-running",
                                             state: "RUNNING", liveness: "NOT_PROVEN")
        let row = try XCTUnwrap(NetworkObservationAdapter.project([stale], canonical: []).first)
        XCTAssertEqual(row.monitorStatus, .running)
        XCTAssertTrue(row.codexRunning)
        XCTAssertEqual(row.stage, "RUNNING")
        XCTAssertEqual(row.displayStage, "RUNNING")
        XCTAssertEqual(row.nativeUpdateNotice, "更新延迟 · 显示上次状态")
    }

    func testRunningWithExplicitLivenessProofCanEnterActive() throws {
        let live = try projectedObservation(node: "p620", host: "P620", thread: "live-running",
                                            state: "RUNNING", liveness: "PROVEN")
        let row = try XCTUnwrap(NetworkObservationAdapter.project([live], canonical: []).first)
        XCTAssertEqual(row.monitorStatus, .running)
        XCTAssertTrue(row.codexRunning)
        XCTAssertEqual(row.displayStage, "RUNNING")
        var expiredCache = row
        expiredCache.observationLivenessValidUntil = Date().timeIntervalSince1970 - 1
        XCTAssertEqual(expiredCache.monitorStatus, .running, "A collection timeout must not change Codex state")
        XCTAssertNotNil(expiredCache.nativeUpdateNotice)
    }

    func testRunningNativeTasksBeyondHistoryWindowAreFetchedFirst() async throws {
        var items: [NetworkObservation] = []
        for index in 0..<350 {
            items.append(try projectedObservation(node: "p620", host: "P620", thread: "history-\(index)", state: "COMPLETED"))
        }
        items.append(try projectedObservation(node: "p620", host: "P620", thread: "dock", state: "RUNNING", liveness: "PROVEN"))
        items.append(try projectedObservation(node: "p620", host: "P620", thread: "spcx", state: "RUNNING", liveness: "PROVEN"))
        let service = UnifiedProjectionService(native: items, paginated: true)
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()
        XCTAssertEqual(store.active.count, 2)
        XCTAssertEqual(Set(store.active.compactMap(\.observationId)), Set(items.suffix(2).map(\.observationId)))
        XCTAssertEqual(store.allTasks.count, 500 > items.count ? items.count : 500)
    }

    func testLiveNativeConversationSupersedesOnlyExactAdoption() async throws {
        let fixture = try canonicalFixture()
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(fixture)) as? [String: Any])
        raw["execution_ref"] = NSNull(); raw["state"] = "QUEUED"; raw["stage"] = "QUEUED"
        raw["execution_state"] = "UNKNOWN"; raw["codex_running"] = false
        raw["native_conversation"] = ["node_id": "p620", "provider": "codex_app_server", "thread_id": "spcx"]
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let adoption = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertEqual(adoption.withEvents([], coverage: "PARTIAL", hasMore: false).nativeConversation?.threadId, "spcx")
        let live = try projectedObservation(node: "p620", host: "P620", thread: "spcx", state: "RUNNING", taskRef: adoption.taskRef, liveness: "PROVEN")
        let unrelated = try projectedObservation(node: "air", host: "Air", thread: "spcx", state: "RUNNING", liveness: "PROVEN")
        XCTAssertFalse(NetworkObservationAdapter.supersedesAdoption(adoption, observations: [unrelated]))
        XCTAssertFalse(NetworkObservationAdapter.supersedesAdoption(fixture, observations: [live]))
        let service = UnifiedProjectionService(canonical: [adoption], native: [live])
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()
        XCTAssertEqual(store.allTasks.count, 1)
        XCTAssertEqual(store.allTasks.first?.monitorStatus, .running)
        XCTAssertNil(store.allTasks.first?.executionRef)
        XCTAssertEqual(store.allTasks.first?.observationId, live.observationId)
        let stale = try projectedObservation(node: "p620", host: "P620", thread: "spcx", state: "RUNNING")
        XCTAssertFalse(NetworkObservationAdapter.supersedesAdoption(adoption, observations: [stale]))
        var terminalRaw = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(live)) as? [String: Any])
        var turn = try XCTUnwrap(terminalRaw["turn"] as? [String: Any])
        turn["native_state"] = "COMPLETED"; turn["completed_at"] = ISO8601DateFormatter().string(from: Date())
        turn["ordinal"] = Int64(Date().timeIntervalSince1970 * 1000)
        terminalRaw["turn"] = turn; terminalRaw["liveness"] = "NOT_PROVEN"
        let completed = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: terminalRaw))
        XCTAssertTrue(NetworkObservationAdapter.supersedesAdoption(adoption, observations: [completed]))
        let completedStore = MonitorStore(service: UnifiedProjectionService(canonical: [adoption], native: [completed], paginated: true),
            defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await completedStore.refresh()
        XCTAssertEqual(completedStore.allTasks.count, 1)
        XCTAssertEqual(completedStore.allTasks.first?.monitorStatus, .completed)

    }

    func testNativeTerminalAndTransportUncertaintyRemainDistinct() throws {
        for state in ["COMPLETED", "CANCELLED", "INTERRUPTED", "TIMED_OUT", "DISCONNECTED", "UNKNOWN"] {
            let observation = try projectedObservation(node: "air", host: "Air", thread: state, state: state)
            let row = try XCTUnwrap(NetworkObservationAdapter.project([observation], canonical: []).first)
            XCTAssertNotEqual(row.monitorStatus, .running)
            XCTAssertEqual(row.monitorStatus == .completed || row.monitorStatus == .cancelled,
                           ["COMPLETED", "CANCELLED", "INTERRUPTED"].contains(state))
            if ["TIMED_OUT", "DISCONNECTED"].contains(state) {
                XCTAssertFalse(observation.turn.isTerminal)
                XCTAssertEqual(row.monitorStatus, .unknown)
            }
            if state == "INTERRUPTED" {
                XCTAssertEqual(row.state, "INTERRUPTED")
                XCTAssertEqual(row.statusLabel, "Interrupted")
                XCTAssertEqual(row.rowTail(status: row.monitorStatus), "Interrupted")
            }
        }
    }

    func testExpiredLiveProofPreservesLifecycleAndActiveMembershipWithoutFalseProgress() async throws {
        let fixture = try projectedObservation(node: "air", host: "Air", thread: "pending", state: "RUNNING",
            taskRef: "task-managed", liveness: "PROVEN", executionRef: "exec-managed")
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(fixture)) as? [String: Any])
        var turn = try XCTUnwrap(raw["turn"] as? [String: Any])
        turn["execution_state"] = "RUNNING"; raw["turn"] = turn
        raw["source_updated_at"] = ""
        raw["liveness_valid_until"] = Date().timeIntervalSince1970 - 1
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let expired = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertTrue(expired.isRunning, "Expired proof must not rewrite Codex inProgress")
        let row = try XCTUnwrap(NetworkObservationAdapter.project([expired], canonical: []).first)
        XCTAssertEqual(row.executionState, "RUNNING")
        XCTAssertEqual(row.monitorStatus, .running)
        XCTAssertEqual(row.nativeUpdateNotice, "更新延迟 · 显示上次状态")
        XCTAssertEqual(row.displayStage, "RUNNING")
        XCTAssertNil(row.timestamps.lastProgressAt)
        let service = UnifiedProjectionService(native: [expired])
        let store = MonitorStore(service: service, defaults: UserDefaults(suiteName: UUID().uuidString)!)
        await store.refresh()
        XCTAssertEqual(store.active.map(\.taskRef), ["task-managed"])
        XCTAssertTrue(store.recent.isEmpty)
        store.view = .active
        XCTAssertEqual(store.visibleTasks.map(\.taskRef), ["task-managed"])
        XCTAssertEqual(store.counts[.active], 1)
        XCTAssertEqual(store.groupedTasks.first?.title, "Running")
        turn["execution_state"] = "SUCCEEDED"; raw["turn"] = turn
        let terminal = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
        let completed = try XCTUnwrap(NetworkObservationAdapter.project([terminal], canonical: []).first)
        XCTAssertEqual(completed.executionState, "SUCCEEDED")
        XCTAssertEqual(completed.monitorStatus, .running, "CLINX finalization must not overwrite the Codex turn status")
        turn["native_state"] = "COMPLETED"; raw["turn"] = turn
        let nativeTerminal = try decoder.decode(NetworkObservation.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertEqual(NetworkObservationAdapter.project([nativeTerminal], canonical: []).first?.monitorStatus, .completed)
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
    func testNativeActivityLoadsRecordsAndSourceTextWithoutExecutionIdentity() async throws {
        let detail = try networkFixture()
        let service = RecordedNativeActivity(detail: detail)
        let store = NetworkObservationStore(client: service)
        await store.select(detail.item.id)
        await store.loadContext()
        XCTAssertEqual(store.activity.map(\.text), ["Recorded response"])
        XCTAssertNil(store.activity.first?.executionRef)
        XCTAssertEqual(store.activityCoverage, "RECEIVED_EVENTS_ONLY")
        XCTAssertEqual(store.renderedHistory.updates.map(\.content.text), ["Recorded response"])
        XCTAssertEqual(store.renderedHistory.response?.text, "Full source response")
        XCTAssertEqual(store.context?.lastCodexResult, "Full source response")
        XCTAssertEqual(store.context?.nextCursor, "earlier-context")

        await store.loadActivity()
        XCTAssertEqual(store.activity.map(\.id), ["new", "old"])
        XCTAssertEqual(store.activity.last?.text, "Earlier response")
        XCTAssertEqual(store.renderedHistory.updates.map(\.content.text), ["Earlier response", "Recorded response"])
        XCTAssertNil(store.activityCursor)
        XCTAssertTrue(store.pausedHistoryRefresh)
        let requested = await service.requestedIDs
        XCTAssertTrue(requested.allSatisfy { $0 == detail.item.observationId })
    }

    func testEarlierNativeContextPreservesBrowsingPositionDuringPolling() async throws {
        let detail = try networkFixture()
        let store = NetworkObservationStore(client: RecordedNativeActivity(detail: detail))
        await store.select(detail.item.id)
        await store.loadContext()
        await store.loadContext(older: true)
        XCTAssertEqual(store.context?.lastCodexResult, "Earlier source response")
        XCTAssertNil(store.context?.nextCursor)
        XCTAssertTrue(store.pausedHistoryRefresh)
    }

    func testDelayedSelectionCannotContaminateAnotherThread() async throws {
        let first = try networkFixture(id: "obs_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased() + "00000000", turn: "turn-a")
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
        XCTAssertTrue(store.renderedHistory.updates.isEmpty)
        XCTAssertNil(store.renderedHistory.response)
    }
}

private actor RecordedNativeActivity: NetworkObservationServing {
    let detail: ObservationDetail
    private(set) var requestedIDs: [String] = []
    init(detail: ObservationDetail) { self.detail = detail }
    func observations(filters: ObservationFilters, cursor: String?) async throws -> ObservationPage {
        throw MonitorError.invalidResponse // Opening Activity must not reload the task catalogue.
    }
    func observation(_ id: String, cursor: String?) async throws -> ObservationDetail {
        requestedIDs.append(id)
        return detail
    }
    func observationActivity(_ id: String, cursor: String?) async throws -> ObservationActivityPage {
        requestedIDs.append(id)
        let older = cursor != nil
        return ObservationActivityPage(schemaVersion: "clinx-observation-v1", observationId: id,
            items: [ObservationActivityEntry(eventId: older ? "old" : "new", turnId: "native-turn",
                executionRef: nil, sourceSeq: older ? 1 : 2, recordedAt: older ? 100 : 200,
                nativeState: "COMPLETED", businessResult: nil, kind: "result",
                text: older ? "Earlier response" : "Recorded response", artifacts: [])],
            coverage: "RECEIVED_EVENTS_ONLY", nextCursor: older ? nil : "earlier-activity", hasMore: !older)
    }
    func observationContext(_ id: String, cursor: String?) async throws -> ObservationContext {
        requestedIDs.append(id)
        return ObservationContext(schemaVersion: "clinx-observation-v1", contextStatus: "AVAILABLE",
            lastUserIntent: "Original user request", lastCodexResult: cursor == nil ? "Full source response" : "Earlier source response",
            nextCursor: cursor == nil ? "earlier-context" : nil)
    }
}

final class ObservationHistoryRendererTests: XCTestCase {
    private func entry(_ receipt: String, turn: String = "turn-a", text: String = "**Native text**") -> ObservationActivityEntry {
        ObservationActivityEntry(eventId: receipt, turnId: turn, executionRef: nil,
            sourceSeq: 1, recordedAt: 100, nativeState: "COMPLETED", businessResult: nil,
            kind: "result", text: text, artifacts: [])
    }

    func testRepeatedReceiptsReuseParsingAndKeepStableRowIdentity() async {
        let renderer = ObservationHistoryRenderer()
        let first = await renderer.render(scope: "obs-a", activity: [entry("receipt-1")], response: "**Native text**")
        let second = await renderer.render(scope: "obs-a", activity: [entry("receipt-2")], response: "**Native text**")
        let count = await renderer.parseCount
        XCTAssertEqual(count, 1, "Polling and context must reuse the same immutable Markdown parse")
        XCTAssertEqual(first, second, "A collection receipt is not a new visible message")
        let differentTurn = await renderer.render(scope: "obs-a", activity: [entry("receipt-3", turn: "turn-b")], response: nil)
        XCTAssertNotEqual(first.updates.first?.id, differentTurn.updates.first?.id)
        let differentSource = await renderer.render(scope: "obs-b", activity: [entry("receipt-1")], response: nil)
        XCTAssertNotEqual(first.updates.first?.id, differentSource.updates.first?.id)
    }

    func testBoundedHistoryKeepsEveryDistinctTextAndDropsEvictedParseCache() async {
        let renderer = ObservationHistoryRenderer()
        let entries = (0..<20).map { entry("receipt-\($0)", text: "## Update \($0)\n\n| Column | Value |\n| --- | --- |\n| original | `\($0)` |") }
        let first = await renderer.render(scope: "obs", activity: entries + entries, response: nil)
        XCTAssertEqual(first.updates.map(\.content.text), entries.reversed().map { $0.text! })
        let firstCount = await renderer.parseCount
        XCTAssertEqual(firstCount, 20)
        _ = await renderer.render(scope: "obs", activity: [], response: nil)
        _ = await renderer.render(scope: "obs", activity: entries, response: nil)
        let secondCount = await renderer.parseCount
        XCTAssertEqual(secondCount, 40, "Text outside the current bounded page must not be retained forever")
    }
}
