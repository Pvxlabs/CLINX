import Foundation
import XCTest
@testable import CLINXMonitor

private func responseObject(_ name: String) throws -> [String: Any] {
    let file = try XCTUnwrap(Bundle.module.url(forResource: "api-examples", withExtension: "json"))
    let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
    XCTAssertEqual(root["synthetic_fixture"] as? Bool, true)
    let examples = try XCTUnwrap(root["examples"] as? [[String: Any]])
    let record = try XCTUnwrap(examples.first { $0["name"] as? String == name })
    return try XCTUnwrap(record["response"] as? [String: Any])
}

private func example<T: Decodable>(_ name: String, as type: T.Type) throws -> T {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: responseObject(name)))
}

final class MonitorModelTests: XCTestCase {
    func testWireExamplesAndStateSafety() throws {
        let health = try example("health", as: ObserverHealth.self)
        XCTAssertEqual(health.authorityLiveness, "UNKNOWN")
        let active = try example("active", as: TaskPage.self)
        XCTAssertEqual(active.items.first?.displayState, .running)
        XCTAssertNil(active.items.first?.progressPercent)
        XCTAssertEqual(active.items.first?.progressBasis, "NO_PERSISTED_DENOMINATOR")
        let passed = try example("pass_detail", as: ObservedTask.self)
        XCTAssertEqual(passed.displayState, .passed)
        XCTAssertEqual(passed.finalResult?.executionRef, passed.executionRef)
        let blocked = try example("blocked_detail", as: ObservedTask.self)
        XCTAssertEqual(blocked.displayState, .blocked) // A historical PASS does not override BLOCKED.
        let unknown = try example("unknown_detail", as: ObservedTask.self)
        XCTAssertEqual(unknown.displayState, .unknown)
        let events = try example("events", as: EventPage.self)
        XCTAssertEqual(events.coverage, "PARTIAL")
        XCTAssertEqual(events.items.count, 2)
    }

    func testFailedCancelledAndMismatchedExecutionNeverPass() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        var record = try responseObject("pass_detail")
        record["state"] = "FAILED"
        var task = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: record))
        XCTAssertEqual(task.displayState, .blocked)
        record["state"] = "CANCELLED"
        task = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: record))
        XCTAssertEqual(task.displayState, .idle)
        record["state"] = "COMPLETED"
        var result = try XCTUnwrap(record["final_result"] as? [String: Any])
        result["execution_ref"] = "different_execution"
        record["final_result"] = result
        task = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: record))
        XCTAssertEqual(task.displayState, .idle)
    }

    func testInvalidEndpointIsRejected() {
        for value in ["http://example.test", "https://user@example.test", "https://example.test/path",
                      "https://example.test/?q=sample"] {
            XCTAssertThrowsError(try ObserverClient(baseURL: URL(string: value)!))
        }
    }
}

private final class StubProtocol: URLProtocol {
    static var status = 200
    static var payload = Data()
    static var observedMethod: String?
    static var observedAuthorization: String?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.observedMethod = request.httpMethod
        Self.observedAuthorization = request.value(forHTTPHeaderField: "Authorization")
        let response = HTTPURLResponse(url: request.url!, statusCode: Self.status,
                                       httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Self.payload)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

final class MonitorClientTests: XCTestCase {
    private func client() throws -> ObserverClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: configuration)
        return try ObserverClient(baseURL: URL(string: "https://observer.example.test")!,
                                  session: session, credential: { String(repeating: "x", count: 32) })
    }

    func testGetOnlyAndAuthorization() async throws {
        let health: ObserverHealth = try example("health", as: ObserverHealth.self)
        StubProtocol.status = 200
        StubProtocol.payload = try JSONEncoder.observerWire.encode(health)
        let value = try await client().health()
        XCTAssertEqual(value.schemaVersion, "1")
        XCTAssertEqual(StubProtocol.observedMethod, "GET")
        XCTAssertEqual(StubProtocol.observedAuthorization, "Bearer " + String(repeating: "x", count: 32))
    }

    func testHTTPFailureAndResponseBound() async throws {
        let service = try client()
        StubProtocol.status = 401
        StubProtocol.payload = Data()
        do { _ = try await service.health(); XCTFail("Expected 401") }
        catch MonitorError.server(401) { }
        StubProtocol.status = 200
        StubProtocol.payload = Data(repeating: 65, count: 2 * 1024 * 1024 + 1)
        do { _ = try await service.health(); XCTFail("Expected response bound") }
        catch MonitorError.invalidResponse { }
    }
}

private extension JSONEncoder {
    static var observerWire: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }
}

private actor FixtureService: ObserverServing {
    let healthValue: ObserverHealth
    let taskValue: ObservedTask
    let eventValue: EventPage
    init() throws {
        healthValue = try example("health", as: ObserverHealth.self)
        taskValue = try example("pass_detail", as: ObservedTask.self)
        eventValue = try example("events", as: EventPage.self)
    }
    func health() async throws -> ObserverHealth { healthValue }
    func tasks(active: Bool, offset: Int) async throws -> TaskPage {
        let page: TaskPage = try example("active", as: TaskPage.self)
        // One item on both pages is enough to exercise deduplication and selection.
        return page
    }
    func task(_ ref: String) async throws -> ObservedTask { taskValue }
    func events(_ ref: String, after: String?) async throws -> EventPage { eventValue }
}

final class MonitorStoreTests: XCTestCase {
    @MainActor
    func testRefreshAndEventDeduplication() async throws {
        let service = try FixtureService()
        let store = MonitorStore(service: service)
        await store.refresh()
        XCTAssertEqual(store.connection, .connected)
        XCTAssertEqual(store.active.count, 1)
        await store.select(store.active[0].taskRef)
        XCTAssertEqual(store.selected?.displayState, .passed)
        XCTAssertEqual(store.events.count, 2)
        await store.loadMoreEvents()
        XCTAssertEqual(store.events.count, 2)
        XCTAssertEqual(store.eventCoverage, "PARTIAL")
    }
}
