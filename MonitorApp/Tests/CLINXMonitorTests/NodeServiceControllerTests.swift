import XCTest
@testable import CLINXMonitor

@MainActor
final class NodeServiceControllerTests: XCTestCase {
    func testDegradedRunningHelperIsNotRestartedOrReinstalled() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let plist = folder.appendingPathComponent("service.plist")
        let health = folder.appendingPathComponent("health.json")
        let original = Data("existing custom runtime route".utf8)
        try original.write(to: plist)
        try Data(#"{"status":"degraded","reason":"OBSERVATION_GAP"}"#.utf8).write(to: health)
        var calls: [[String]] = []
        let controller = NodeServiceController(plistURL: plist, healthURL: health) { arguments in
            calls.append(arguments)
            return (0, "state = running\n\tpid = 4242\n")
        }

        controller.installAndEnable()

        XCTAssertEqual(controller.state, .degraded("OBSERVATION_GAP"))
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.first, "print")
        XCTAssertEqual(try Data(contentsOf: plist), original)
        try Data(#"{"status":"running"}"#.utf8).write(to: health)
        controller.retry()
        XCTAssertEqual(controller.state, .running)
        XCTAssertTrue(calls.allSatisfy { $0.first == "print" })
        XCTAssertEqual(try Data(contentsOf: plist), original)
    }

    func testLoadedStoppedHelperStartsWithoutForceRestart() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let health = folder.appendingPathComponent("health.json")
        try Data(#"{"status":"running"}"#.utf8).write(to: health)
        var calls: [[String]] = []
        let controller = NodeServiceController(plistURL: folder.appendingPathComponent("service.plist"), healthURL: health) { arguments in
            calls.append(arguments)
            return (0, "state = waiting\n")
        }

        controller.installAndEnable()

        XCTAssertEqual(controller.state, .running)
        XCTAssertEqual(calls.map { $0.first! }, ["print", "kickstart"])
        XCTAssertFalse(calls.contains { $0.contains("-k") })
        XCTAssertFalse(FileManager.default.fileExists(atPath: folder.appendingPathComponent("service.plist").path))
    }

    func testHealthReasonsArePreservedInsteadOfClaimingPairingFailed() {
        XCTAssertEqual(NodeServiceController.healthState(["status": "blocked", "reason": "SHARING_AUTHORIZATION_REVOKED"]),
                       .requiresApproval("Node service blocked (SHARING_AUTHORIZATION_REVOKED)."))
        XCTAssertEqual(NodeServiceController.healthState(["status": "offline", "reason": "TimeoutError"]),
                       .failed("Node service offline (TimeoutError)."))
        XCTAssertEqual(NodeServiceController.healthState(["status": "stopped"]), .disabled)
        XCTAssertNil(NodeServiceController.healthState(["status": "unrecognized"]))
    }
}
