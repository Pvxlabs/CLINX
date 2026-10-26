import AppKit
import SwiftUI
import XCTest
@testable import CLINXMonitor

@MainActor
final class ActivityLayoutTests: XCTestCase {
    func testRecordedHistoryRealizesVisibleRowsInsteadOfAllTwentyReports() async throws {
        let entries = (0..<20).map { index in
            ObservationActivityEntry(eventId: "receipt-\(index)", turnId: "turn-\(index)",
                executionRef: nil, sourceSeq: Int64(index), recordedAt: Double(index),
                nativeState: "COMPLETED", businessResult: nil, kind: "result",
                text: "## Report \(index)\n\n" + report, artifacts: [])
        }
        let renderer = ObservationHistoryRenderer()
        let prepared = await renderer.render(scope: "layout-fixture", activity: entries, response: nil)
        XCTAssertEqual(prepared.updates.count, 20)
        func check(_ lazy: Bool) throws {
            var appeared = Set<String>()
            let rows = ForEach(prepared.updates) { entry in
                RecordedObservationUpdateView(entry: entry)
                    .onAppear { appeared.insert(entry.id) }
            }
            let content = ScrollView {
                if lazy {
                    LazyVStack(alignment: .leading, spacing: 24) { rows }
                        .frame(maxWidth: ActivityMarkdownView.readingWidth)
                } else {
                    VStack(alignment: .leading, spacing: 24) { rows }
                        .frame(maxWidth: ActivityMarkdownView.readingWidth)
                }
            }
            let view = NSHostingView(rootView: content)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 850, height: 600),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = view
            let start = ContinuousClock.now
            window.layoutIfNeeded()
            view.layoutSubtreeIfNeeded()
            let elapsed = start.duration(to: .now)
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            print("HISTORY_LAYOUT_METRIC lazy=\(lazy) initial=\(elapsed) realized=\(appeared.count)/20")
            if lazy {
                XCTAssertGreaterThan(appeared.count, 0)
                XCTAssertLessThan(appeared.count, 20, "Off-screen reports must not all be realized on expansion")
            } else {
                XCTAssertEqual(appeared.count, 20, "Control reproduces the old eager history layout")
            }
            if let folder = ProcessInfo.processInfo.environment["CLINX_LAYOUT_EVIDENCE_DIR"] {
                try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
                let bitmap = try XCTUnwrap(view.bitmapImageRepForCachingDisplay(in: view.bounds))
                view.cacheDisplay(in: view.bounds, to: bitmap)
                let png = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
                try png.write(to: URL(fileURLWithPath: folder).appendingPathComponent("history-lazy-\(lazy).png"))
            }
            window.contentView = nil
        }
        for lazy in [false, true] { try check(lazy) }
    }

    private let paragraph = "历史 Checkpoint 还显示：20 Cell 的 cursor 推进包含合法 HOLD；BTC/ETH/SOL/ZEC 的 CONT 为 `R3_NO_COVERED_CANDIDATES`，SUI/CONT 有 1 个 deferred candidate。相关 Adapter fail-closed、scope 隔离及 R3 dependency 回归共 9 项通过。现在将结果写入已有 canonical 文档，明确本轮阻塞、历史可复用范围和最小后续读取需求。"

    private var report: String {
        "**P1.6 本轮可达验收结论为 BLOCKED，生产继续 NOT_DEPLOYED。**\n\n" +
        Array(repeating: paragraph, count: 6).joined(separator: "\n\n") +
        "\n\n| 验收项 | 结果 |\n| --- | --- |\n| REAL_DATA_20_CELL | BLOCKED |\n| SOURCE_ACK_CRASH | PASS |\n\n" +
        "```text\nLIQUIDATION_GAP_RCA=BLOCKED\nLIVE_QUALIFICATION=BLOCKED\n```"
    }

    private func host(width: CGFloat, size: Double = 15) -> NSHostingView<some View> {
        let defaults = UserDefaults(suiteName: "CLINXMonitorTests.layout.\(UUID().uuidString)")!
        defaults.set(size, forKey: "monitor.text.contentSize")
        return NSHostingView(rootView:
            VStack(alignment: .leading, spacing: 24) {
                Text("Codex · Final response 09:48:25").font(.caption).foregroundStyle(.black)
                ActivityMarkdownView(text: report)
                Text("Recorded feedback · execution status is shown above").font(.caption).foregroundStyle(.black)
            }
            .frame(width: width, alignment: .leading)
            .defaultAppStorage(defaults)
            .background(Color.white)
            .environment(\.colorScheme, .light))
    }

    func testChineseMarkdownReservesFullHeightWhenWidthAndFontChange() {
        let wide = host(width: 800).fittingSize.height
        let narrow = host(width: 400).fittingSize.height
        let larger = host(width: 400, size: 24).fittingSize.height
        XCTAssertGreaterThan(wide, 400)
        XCTAssertGreaterThan(narrow, wide + 100)
        XCTAssertGreaterThan(larger, narrow + 200)
    }

    func testConstrainedTranscriptDoesNotCompressMarkdownOverAdjacentLabels() {
        let text = report
        let reference = NSHostingView(rootView:
            ActivityMarkdownView(text: text)
                .fixedSize(horizontal: false, vertical: true).frame(width: 480))
        let required = reference.fittingSize.height
        var frames: [String: CGRect] = [:]
        let transcript = NSHostingView(rootView:
            VStack(alignment: .leading, spacing: 24) {
                Text("Codex · Final response").font(.caption)
                ActivityMarkdownView(text: text)
                    .background(GeometryReader { geometry in
                        Color.clear.preference(key: TranscriptFrames.self,
                            value: ["body": geometry.frame(in: .named("transcript"))])
                    })
                Text("Recorded feedback · execution status is shown above").font(.caption)
                    .background(GeometryReader { geometry in
                        Color.clear.preference(key: TranscriptFrames.self,
                            value: ["footer": geometry.frame(in: .named("transcript"))])
                    })
            }
            .coordinateSpace(name: "transcript")
            .onPreferenceChange(TranscriptFrames.self) { frames = $0 })
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 300),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        defer { window.contentView = nil }
        window.contentView = transcript
        window.layoutIfNeeded()
        transcript.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        let body = frames["body"] ?? .zero
        let footer = frames["footer"] ?? .zero
        XCTAssertGreaterThanOrEqual(body.height, required - 1,
            "A compressed Markdown container lets its full-height paragraphs draw over the next label")
        XCTAssertGreaterThanOrEqual(footer.minY, body.maxY + 20)
    }

    func testCaptureChineseTranscriptAtNarrowAndWideWidths() throws {
        guard let folder = ProcessInfo.processInfo.environment["CLINX_LAYOUT_EVIDENCE_DIR"] else { return }
        try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
        for width: CGFloat in [400, 800] {
            for size: Double in [15, 24] {
                let view = host(width: width, size: size)
                view.frame = NSRect(origin: .zero, size: view.fittingSize)
                view.layoutSubtreeIfNeeded()
                let bitmap = try XCTUnwrap(view.bitmapImageRepForCachingDisplay(in: view.bounds))
                view.cacheDisplay(in: view.bounds, to: bitmap)
                let data = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
                try data.write(to: URL(fileURLWithPath: folder)
                    .appendingPathComponent("transcript-\(Int(width))-\(Int(size)).png"))
            }
        }
    }
}

private struct TranscriptFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, latest in latest })
    }
}
