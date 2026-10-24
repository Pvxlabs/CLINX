import Foundation
import Darwin

/// User-session node helper lifecycle.  The Monitor window never owns the
/// helper process: closing the window leaves a previously enabled LaunchAgent
/// running.  Installation is explicit and reports system approval failures.
@MainActor
final class NodeServiceController: ObservableObject {
    enum State: Equatable {
        case notInstalled
        case disabled
        case starting
        case running
        case degraded(String)
        case failed(String)
        case requiresApproval(String)

        var label: String {
            switch self {
            case .notInstalled: return "Node service not installed"
            case .disabled: return "Node service disabled"
            case .starting: return "Starting node service…"
            case .running: return "Node service running"
            case .degraded(let reason): return "Node service running · observation degraded (\(reason))"
            case .failed(let message), .requiresApproval(let message): return message
            }
        }
    }

    @Published private(set) var state: State = .notInstalled
    @Published private(set) var lastError: String?

    private let label = "com.pvxlabs.clinx.node"
    private let plistURL: URL
    private let healthURL: URL
    private let commandRunner: (([String]) -> (status: Int32, output: String))?

    init(plistURL: URL? = nil, healthURL: URL? = nil,
         commandRunner: (([String]) -> (status: Int32, output: String))? = nil) {
        self.plistURL = plistURL ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            .appendingPathComponent("\(label).plist")
        self.healthURL = healthURL ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CLINX Monitor/Node", isDirectory: true)
            .appendingPathComponent("health.json")
        self.commandRunner = commandRunner
    }

    func refresh() {
        guard FileManager.default.fileExists(atPath: plistURL.path) else {
            state = .notInstalled
            return
        }
        let result = launchctl(["print", "gui/\(getuid())/\(label)"])
        guard result.status == 0 else {
            state = .disabled
            return
        }
        state = helperHealth() ?? .starting
    }

    func installAndEnable() {
        lastError = nil
        let job = "gui/\(getuid())/\(label)"
        let loaded = launchctl(["print", job])
        if loaded.status == 0 {
            // Refresh an active helper without overwriting its configured route
            // or interrupting work. A loaded, stopped job only needs kickstart.
            if loaded.output.range(of: #"\bpid = [1-9][0-9]*\b"#, options: .regularExpression) != nil {
                state = helperHealth() ?? .starting
            } else {
                _ = launchctl(["kickstart", job])
                state = waitForHealth() ?? .starting
            }
            return
        }
        guard let executable = Bundle.main.url(forResource: "CLINXNodeService", withExtension: nil) else {
            state = .failed("This app build does not contain the CLINX node service.")
            return
        }
        do {
            try FileManager.default.createDirectory(at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            let plist: [String: Any] = [
                "Label": label,
                "ProgramArguments": [executable.path, "--node-service"],
                "RunAtLoad": true,
                "KeepAlive": ["SuccessfulExit": false, "NetworkState": true],
                "ProcessType": "Interactive",
                "ThrottleInterval": 5,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            ]
            let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
            try data.write(to: plistURL, options: .atomic)
        } catch {
            state = .failed("Could not install the user-session node service: \(error.localizedDescription)")
            return
        }
        state = .starting
        let result = launchctl(["bootstrap", "gui/\(getuid())", plistURL.path])
        if result.status == 0 {
            state = waitForHealth() ?? .starting
        } else if launchctl(["print", "gui/\(getuid())/\(label)"]).status == 0 {
            // bootstrap is intentionally idempotent at the UI boundary: a
            // previously installed LaunchAgent may already be loaded while
            // its helper is stopped after a clean exit.  Treat that as a
            // restart, not as a macOS approval failure.
            _ = launchctl(["kickstart", job])
            state = waitForHealth() ?? .starting
        } else {
            state = .requiresApproval("macOS did not approve the node helper. Open System Settings, allow CLINX, then retry.")
            lastError = result.output
        }
    }

    func disable() {
        _ = launchctl(["bootout", "gui/\(getuid())/\(label)"])
        state = .disabled
    }

    func retry() { installAndEnable() }

    private func waitForHealth() -> State? {
        for _ in 0..<10 {
            if let status = helperHealth(), status != .disabled { return status }
            usleep(200_000)
        }
        return nil
    }

    private func helperHealth() -> State? {
        guard let data = try? Data(contentsOf: healthURL),
              let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any] else { return nil }
        return Self.healthState(payload)
    }

    static func healthState(_ payload: [String: Any]) -> State? {
        let reason = payload["reason"] as? String ?? "UNKNOWN"
        switch payload["status"] as? String {
        case "running": return .running
        case "degraded": return .degraded(reason)
        case "blocked": return .requiresApproval("Node service blocked (\(reason)).")
        case "offline": return .failed("Node service offline (\(reason)).")
        case "stopped": return .disabled
        default: return nil
        }
    }

    private func launchctl(_ arguments: [String]) -> (status: Int32, output: String) {
        if let commandRunner { return commandRunner(arguments) }
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            process.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
        } catch {
            return (1, error.localizedDescription)
        }
    }
}
