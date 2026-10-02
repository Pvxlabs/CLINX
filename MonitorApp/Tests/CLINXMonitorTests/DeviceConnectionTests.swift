import XCTest
@testable import CLINXMonitor

final class DeviceConnectionTests: XCTestCase {
    @MainActor
    func testDisconnectDiscardsAnInFlightResponseFromPreviousDevice() async {
        let name = "DeviceConnectionTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let service = DelayedDeviceObserver()
        let store = MonitorStore(service: service, defaults: defaults)
        let oldRequest = Task { await store.refresh() }
        while !(await service.waiting) { await Task.yield() }
        store.disconnectPairedDevice()
        await service.finish()
        await oldRequest.value
        XCTAssertNil(store.health)
        XCTAssertTrue(store.allTasks.isEmpty)
        XCTAssertFalse(store.isRefreshing)
        XCTAssertNil(store.errorCategory)
    }
    private func response(node: String = "p620", readOnly: Bool = true,
                          endpoint: String = "https://p620.example.ts.net", credential: String = String(repeating: "x", count: 48)) throws -> DeviceResponse {
        let data = try JSONSerialization.data(withJSONObject: [
            "node_id": node, "read_only": readOnly, "endpoint": endpoint, "credential": credential
        ])
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(DeviceResponse.self, from: data)
    }

    func testBootstrapRequiresExactPairedIdentityAndReadScope() throws {
        XCTAssertNoThrow(try DeviceBootstrap(reply: response(), expectedNodeID: "p620"))
        XCTAssertThrowsError(try DeviceBootstrap(reply: response(node: "other"), expectedNodeID: "p620"))
        XCTAssertThrowsError(try DeviceBootstrap(reply: response(readOnly: false), expectedNodeID: "p620"))
    }

    func testBootstrapRejectsCredentialsInURLAndNonHTTPS() throws {
        for endpoint in ["http://p620.local", "https://user@p620.local", "https://p620.local/path", "https://p620.local?q=x"] {
            XCTAssertThrowsError(try DeviceBootstrap(reply: response(endpoint: endpoint), expectedNodeID: "p620"))
        }
        XCTAssertThrowsError(try DeviceBootstrap(reply: response(credential: "short"), expectedNodeID: "p620"))
    }

    @MainActor
    func testRelaunchRestoresDeviceCredentialAccountAndDisconnectPreservesManualRoute() {
        let name = "DeviceConnectionTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set("https://p620.example.ts.net", forKey: "monitor.endpoint")
        defaults.set("p620", forKey: "monitor.deviceID")
        defaults.set("P620", forKey: "monitor.deviceName")
        defaults.set("https://manual.example.ts.net", forKey: "monitor.manualEndpoint")
        let store = MonitorStore(defaults: defaults)
        XCTAssertEqual(store.credentialAccount, "device:p620")
        XCTAssertEqual(store.linkedDeviceName, "P620")
        store.disconnectPairedDevice()
        XCTAssertNil(store.linkedDeviceID)
        XCTAssertEqual(store.endpointText, "")
        XCTAssertEqual(store.manualEndpoint, "https://manual.example.ts.net")
        XCTAssertEqual(store.credentialAccount, "p620-observer")
        XCTAssertNil(defaults.object(forKey: "monitor.deviceID"))
    }
}

private actor DelayedDeviceObserver: ObserverServing {
    private var continuation: CheckedContinuation<Void, Never>?
    var waiting: Bool { continuation != nil }
    private let fixture = SyntheticObserverService(scenario: .running)
    func health() async throws -> ObserverHealth {
        await withCheckedContinuation { continuation = $0 }
        return try await fixture.health()
    }
    func finish() { continuation?.resume(); continuation = nil }
    func tasks(active: Bool, offset: Int) async throws -> TaskPage { try await fixture.tasks(active: active, offset: offset) }
    func task(_ ref: String) async throws -> ObservedTask { try await fixture.task(ref) }
    func events(_ ref: String, after: String?) async throws -> EventPage { try await fixture.events(ref, after: after) }
}
