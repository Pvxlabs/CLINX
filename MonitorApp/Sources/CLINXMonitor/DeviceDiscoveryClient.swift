import Foundation

struct NearbyDevice: Decodable, Identifiable, Equatable, Sendable {
    let nodeId: String
    let displayName: String
    let state: String
    let securityStatus: String?
    var id: String { nodeId }
    var paired: Bool { securityStatus == "OPAQUE_V1" }
    var online: Bool { state == "Online Trusted" || state == "Online Unpaired" }
    var identityMismatch: Bool { state == "Identity Mismatch" }
}

struct DeviceResponse: Decodable, Sendable {
    var devices: [NearbyDevice]?
    var backend: String?
    var error: String?
    var endpoint: String?
    var credential: String?
    var nodeId: String?
    var displayName: String?
    var readOnly: Bool?
}

enum DeviceConnectionError: Error, LocalizedError {
    case runtimeUnavailable, timeout, failed(String)
    var errorDescription: String? {
        switch self {
        case .runtimeUnavailable: return "Device pairing is not installed on this Mac. Run the discovery runtime setup included with CLINX."
        case .timeout: return "The device did not respond in time. Check that it is online and try again."
        case .failed(let code):
            switch code {
            case "DEVICE_OFFLINE", "DEVICE_UNREACHABLE": return "This device is offline. Connect both devices to the same local network and try again."
            case "IDENTITY_MISMATCH": return "This device’s identity has changed. Verify the device before pairing again."
            case "OBSERVER_SHARING_UNAVAILABLE": return "The device is paired, but Monitor sharing is not enabled on it yet. Enable sharing on that device, then connect again."
            case "PRODUCTION_PAKE_UNAVAILABLE", "PRODUCTION_PAKE_VERSION_MISMATCH": return "The secure pairing runtime is unavailable. Reinstall the discovery runtime."
            case "REMOTE_REQUEST_REJECTED", "INVALID_PAIRING_CODE": return "Pairing was not accepted. Open a new pairing window on the other device and enter its four-digit code."
            default: return "Could not connect to this device. Check its pairing window and local network, then try again."
            }
        }
    }
}

/// Each operation owns one short-lived helper. Its pipes are private, bounded, and
/// never logged; blocking discovery and crypto work stays off the main actor.
struct DeviceDiscoveryClient: Sendable {
    func request(_ operation: String, nodeID: String? = nil, pin: String = "") async throws -> DeviceResponse {
        let runner = DiscoveryProcess()
        return try await withTaskCancellationHandler {
            try await Task.detached(priority: .userInitiated) {
                try runner.run(operation: operation, nodeID: nodeID, pin: pin)
            }.value
        } onCancel: { runner.cancel() }
    }
}

private final class DiscoveryProcess: @unchecked Sendable {
    private let lock = NSLock()
    private let process = Process()
    private var cancelled = false

    func cancel() {
        lock.lock()
        cancelled = true
        if process.isRunning { process.terminate() }
        lock.unlock()
    }

    func run(operation: String, nodeID: String?, pin: String) throws -> DeviceResponse {
        let runtime = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CLINX Monitor/DiscoveryRuntime/bin/python")
        guard FileManager.default.isExecutableFile(atPath: runtime.path),
              let root = Bundle.main.resourceURL,
              FileManager.default.fileExists(atPath: root.appendingPathComponent("Discovery/local_discovery/monitor_helper.py").path)
        else { throw DeviceConnectionError.runtimeUnavailable }
        let input = Pipe(), output = Pipe()
        process.executableURL = runtime
        process.arguments = ["-I", "-B", root.appendingPathComponent("Discovery/local_discovery/monitor_helper.py").path]
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        var request = ["operation": operation]
        if let nodeID { request["node_id"] = nodeID }
        var payload = try JSONSerialization.data(withJSONObject: request)
        payload.append(0x0a)
        if operation == "pair" { payload.append(Data((pin + "\n").utf8)) }

        lock.lock()
        if cancelled { lock.unlock(); throw CancellationError() }
        do {
            try process.run()
            try input.fileHandleForWriting.write(contentsOf: payload)
            input.fileHandleForWriting.closeFile()
        } catch {
            if process.isRunning { process.terminate() }
            lock.unlock()
            throw DeviceConnectionError.runtimeUnavailable
        }
        lock.unlock()
        let timeout = DispatchWorkItem { [weak self] in self?.cancel() }
        DispatchQueue.global().asyncAfter(deadline: .now() + 25, execute: timeout)
        defer { timeout.cancel(); input.fileHandleForWriting.closeFile(); output.fileHandleForReading.closeFile() }
        var data = Data()
        while true {
            let chunk = output.fileHandleForReading.readData(ofLength: 4096)
            if chunk.isEmpty { break }
            data.append(chunk)
            if data.count > 65536 { cancel(); throw DeviceConnectionError.failed("INVALID_RESPONSE") }
        }
        process.waitUntilExit()
        lock.lock()
        let wasCancelled = cancelled
        lock.unlock()
        if wasCancelled { throw DeviceConnectionError.timeout }
        guard process.terminationStatus == 0 else { throw DeviceConnectionError.runtimeUnavailable }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(DeviceResponse.self, from: data)
        if let error = response.error { throw DeviceConnectionError.failed(error) }
        return response
    }
}
