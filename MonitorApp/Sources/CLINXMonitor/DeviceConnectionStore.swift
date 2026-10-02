import Foundation

@MainActor
final class DeviceConnectionStore: ObservableObject {
    @Published private(set) var devices: [NearbyDevice] = []
    @Published private(set) var scanning = false
    @Published private(set) var busyDeviceID: String?
    @Published private(set) var phase: String?
    @Published private(set) var error: String?
    @Published private(set) var backendReady = false
    @Published private(set) var backendChecked = false
    @Published private(set) var connectedDeviceID: String?
    private let client = DeviceDiscoveryClient()
    private var operation: Task<Void, Never>?
    private var generation = 0

    var peers: [NearbyDevice] { devices.filter { $0.state != "This Device" } }
    var paired: [NearbyDevice] { peers.filter(\.paired) }
    var nearby: [NearbyDevice] { peers.filter { !$0.paired } }

    func scan() async {
        guard !scanning, busyDeviceID == nil else { return }
        scanning = true
        defer { scanning = false }
        do {
            let reply = try await client.request("discover")
            try Task.checkCancellation()
            devices = (reply.devices ?? []).sorted { $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending }
            backendReady = reply.backend == "OPAQUE_V1"
            backendChecked = true
        } catch is CancellationError { }
        catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }

    func connect(_ device: NearbyDevice, pin: String? = nil, monitor: MonitorStore) {
        guard busyDeviceID == nil else { return }
        generation += 1
        let token = generation
        error = nil
        connectedDeviceID = nil
        busyDeviceID = device.id
        phase = pin == nil ? "Connecting securely…" : "Pairing securely…"
        operation = Task {
            do {
                let reply = try await client.request(pin == nil ? "connect" : "pair", nodeID: device.id, pin: pin ?? "")
                try Task.checkCancellation()
                let connection = try DeviceBootstrap(reply: reply, expectedNodeID: device.id)
                phase = "Verifying Monitor connection…"
                let candidate = try ObserverClient(baseURL: connection.url, candidateCredential: connection.credential)
                _ = try await candidate.health()
                try Task.checkCancellation()
                guard generation == token else { return }
                try ObserverKeychain.replace(connection.credential, account: "device:\(device.id)")
                try monitor.configurePaired(endpoint: connection.url.absoluteString, nodeID: device.id, name: device.displayName)
                connectedDeviceID = device.id
                phase = nil
            } catch {
                guard generation == token, !Task.isCancelled else { return }
                if error is MonitorError {
                    self.error = "The device is paired, but its Monitor service could not be verified. Check the host’s Observer service and private network connection, then retry."
                } else { self.error = error.localizedDescription }
                phase = nil
            }
            guard generation == token else { return }
            busyDeviceID = nil
            await scan()
        }
    }

    func cancel() {
        generation += 1
        operation?.cancel()
        operation = nil
        busyDeviceID = nil
        phase = nil
    }

    func clearError() { error = nil; connectedDeviceID = nil }

    func forget(_ device: NearbyDevice, monitor: MonitorStore) async {
        guard busyDeviceID == nil else { return }
        busyDeviceID = device.id
        error = nil
        do {
            // Remove the credential first. Failed trust cleanup remains visible/retryable.
            try ObserverKeychain.revokeLocal(account: "device:\(device.id)")
            if monitor.linkedDeviceID == device.id { monitor.disconnectPairedDevice() }
            _ = try await client.request("forget", nodeID: device.id)
        } catch { self.error = error.localizedDescription }
        busyDeviceID = nil
        await scan()
    }
}

struct DeviceBootstrap {
    let url: URL
    let credential: String
    init(reply: DeviceResponse, expectedNodeID: String) throws {
        guard reply.nodeId == expectedNodeID, reply.readOnly == true,
              let endpoint = reply.endpoint, let url = URL(string: endpoint),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty || url.path == "/",
              let credential = reply.credential,
              credential.range(of: "^[A-Za-z0-9_-]{32,256}$", options: .regularExpression) != nil
        else { throw DeviceConnectionError.failed("INVALID_OBSERVER_CONNECTION") }
        self.url = url
        self.credential = credential
    }
}
