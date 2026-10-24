import AppKit
import SwiftUI
import XCTest
@testable import CLINXMonitor

final class InspectorMetadataTests: XCTestCase {
    private func fixture(_ changes: [String: Any] = [:]) throws -> ObservedTask {
        let original = try XCTUnwrap(SyntheticTasks.all().first { $0.monitorStatus == .running })
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(original)) as? [String: Any])
        object.merge(changes) { _, new in new }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: object))
    }

    func testOverviewKeepsUsefulIdentifiersWithoutRepeatingHeaderOrActivity() throws {
        let task = try fixture(["project": "/home/pvxlabs/dev/HDL/"])
        XCTAssertEqual(task.workspaceName, "HDL")
        XCTAssertEqual(task.projectText, "/home/pvxlabs/dev/HDL/", "Display names must not change routing/filter identity")
        XCTAssertEqual(task.overviewRows.map(\.label),
                       ["TASK ID", "THREAD ID", "MODEL", "PROVIDER", "STARTED"])
        XCTAssertEqual(task.overviewRows.first?.value, task.taskRef)
        XCTAssertEqual(task.overviewRows.first?.copyable, true)
        XCTAssertTrue(task.startedDetailText.contains("UTC+8"))
        let terminal = try fixture(["state": "COMPLETED", "final_result": NSNull()])
        XCTAssertEqual(terminal.overviewRows.last?.label, "RESULT")
        XCTAssertEqual(terminal.overviewRows.last?.value, "NOT REPORTED")
    }

    func testModelDistinguishesActualConfiguredAndUnknown() throws {
        XCTAssertEqual(try fixture(["model": ["resolved": "gpt-6.1-sol", "logical": "default"]]).modelText,
                       "gpt-6.1-sol")
        XCTAssertEqual(try fixture(["model": ["resolved": "", "logical": "default"]]).modelText,
                       "default (configured)")
        XCTAssertEqual(try fixture(["model": ["resolved": NSNull(), "logical": NSNull()]]).modelText,
                       "UNKNOWN")
    }

    @MainActor
    func testClickingValueCopiesTheCompleteIDAndShowsFeedback() async throws {
        let board = NSPasteboard.withUniqueName()
        defer { board.releaseGlobally() }
        let feedback = ClipboardFeedback { value in
            board.clearContents()
            return board.setString(value, forType: .string)
        }
        let value = "task_" + String(repeating: "abcdef0123456789", count: 8)
        let host = NSHostingView(rootView: CopyableMetadataValue(label: "TASK ID", value: value, feedback: feedback)
            .font(DS.Font.mono(12)))
        host.frame = NSRect(x: 0, y: 0, width: 240, height: 40)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = host
        window.orderBack(nil)
        defer { window.orderOut(nil); window.contentView = nil }
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        // SwiftPM's XCTest host does not publish a SwiftUI accessibility tree.
        // Dispatch real local window events to exercise the rendered Button.
        let point = host.convert(NSPoint(x: host.bounds.midX, y: host.bounds.midY), to: nil)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            let event = try XCTUnwrap(NSEvent.mouseEvent(with: type, location: point,
                modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                windowNumber: window.windowNumber, context: nil, eventNumber: 0,
                clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0))
            window.sendEvent(event)
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(board.string(forType: .string), value)
        XCTAssertEqual(feedback.status, .copied)
        host.layoutSubtreeIfNeeded()
        try savePreview(host, name: "copy-feedback")
        try await Task.sleep(nanoseconds: 1_600_000_000)
        XCTAssertEqual(feedback.status, .idle)
    }

    @MainActor
    func testCopyFailureAndRepeatedCopiesNeverShowOldFeedback() async throws {
        let failure = ClipboardFeedback(write: { _ in false })
        failure.copy("thread-id")
        XCTAssertEqual(failure.status, .failed)
        var written: [String] = []
        let feedback = ClipboardFeedback { written.append($0); return true }
        feedback.copy("task-one")
        try await Task.sleep(nanoseconds: 900_000_000)
        feedback.copy("task-two")
        try await Task.sleep(nanoseconds: 900_000_000)
        XCTAssertEqual(feedback.status, .copied, "The first copy's timer must not clear the second copy's feedback")
        XCTAssertEqual(written, ["task-one", "task-two"])
        feedback.clear()
        XCTAssertEqual(feedback.status, .idle)
        failure.clear()
    }

    @MainActor
    func testOverviewRendersAtCompactAndWideWidths() async throws {
        let task = try fixture(["project": "/home/pvxlabs/dev/HDL", "title": "检查 HDL collector 数据供给",
                               "task_ref": "native_observation:node_p620:codex:01a1697c-d4fa-7fd1-913c-d8184dd8f9e5:01a16cc1-2261-7632-b273-e1d0c777c58c",
                               "native_conversation": ["node_id": "p620", "provider": "codex", "thread_id": "01a1697c-d4fa-7fd1-913c-d8184dd8f9e5"],
                               "model": ["resolved": "gpt-6.1-sol", "logical": NSNull()]])
        for width in [440, 900] {
            let host = NSHostingView(rootView: VStack(alignment: .leading, spacing: 20) {
                Text("SYNTHETIC PREVIEW").font(DS.Font.meta).padding(.horizontal, 24)
                ExecutionHeaderView(task: task, stacked: width < 600)
                VStack(alignment: .leading, spacing: 12) {
                    SectionLabel(title: "OVERVIEW")
                    MetadataGrid(rows: task.overviewRows)
                }.padding(.horizontal, 24)
                Spacer()
            }.background(Color(nsColor: .windowBackgroundColor)))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: CGFloat(width), height: 480),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = host
            window.appearance = NSAppearance(named: .aqua)
            window.orderBack(nil)
            defer { window.orderOut(nil); window.contentView = nil }
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 200_000_000)
            XCTAssertEqual(host.bounds.width, CGFloat(width))
            XCTAssertFalse(host.hasAmbiguousLayout)
            try savePreview(host, name: "inspector-\(width)")
        }
    }


    @MainActor
    private func savePreview<V: View>(_ host: NSHostingView<V>, name: String) throws {
        guard let path = ProcessInfo.processInfo.environment["CLINX_UI_TEST_ARTIFACT_DIR"] else { return }
        let directory = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let bitmap = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: bitmap)
        let data = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
        try data.write(to: directory.appendingPathComponent("\(name).png"))
    }
}
