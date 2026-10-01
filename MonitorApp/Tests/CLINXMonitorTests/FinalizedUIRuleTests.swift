import SwiftUI
import XCTest
@testable import CLINXMonitor

/// Pins the UI details finalized in the Figma design file `OgzTpC5hnbctXciUVN6wri`
/// (Live / Healthy `5:3841`, Active `5:4657`, Blocked `5:5531`, Failed `5:6312`).
///
/// Every case guards a rule a later change could silently undo. The two rules that live in a
/// view body rather than in a value — the transparent RuntimeStatus and the absent row edge —
/// are covered by the source-level regression test beside the Python suite.
final class FinalizedUIRuleTests: XCTestCase {

    // MARK: - Runtime status

    func testRuntimeStatusResolvesTheFourFinalizedStates() {
        XCTAssertEqual(RuntimeStatusState.resolve(synthetic: false, connection: .connected), .live)
        XCTAssertEqual(RuntimeStatusState.resolve(synthetic: false, connection: .degraded), .stale)
        XCTAssertEqual(RuntimeStatusState.resolve(synthetic: false, connection: .offline), .offline)
        // Fixtures read as synthetic whatever the Observer link reports.
        XCTAssertEqual(RuntimeStatusState.resolve(synthetic: true, connection: .connected), .synthetic)
        XCTAssertEqual(RuntimeStatusState.resolve(synthetic: true, connection: .offline), .synthetic)
        XCTAssertEqual(RuntimeStatusState.allCases.map(\.rawValue),
                       ["live", "stale", "offline", "synthetic"])
    }

    func testRuntimeStatusLabelsMatchTheDesign() {
        XCTAssertEqual(RuntimeStatusState.live.environment, "P620")
        XCTAssertEqual(RuntimeStatusState.live.connection, "Connected")
        XCTAssertEqual(RuntimeStatusState.stale.environment, "P620")
        XCTAssertEqual(RuntimeStatusState.stale.connection, "Stale")
        XCTAssertEqual(RuntimeStatusState.offline.environment, "P620")
        XCTAssertEqual(RuntimeStatusState.offline.connection, "Offline")
        XCTAssertEqual(RuntimeStatusState.synthetic.environment, "SYNTHETIC")
        XCTAssertEqual(RuntimeStatusState.synthetic.connection, "Local")
    }

    @MainActor
    func testStoreRuntimeStatusFollowsConnectionAndSyntheticMode() async throws {
        let store = MonitorStore()

        store.useSynthetic(.running)
        await store.refresh()
        XCTAssertEqual(store.connection7, .connected)
        XCTAssertEqual(store.runtimeStatus, .synthetic,
                       "a synthetic fixture must never read as live")

        store.useSynthetic(.offline)
        await store.refresh()
        XCTAssertEqual(store.connection7, .offline)
        XCTAssertEqual(store.runtimeStatus, .synthetic,
                       "the fixture marker outranks the Observer link")
    }

    // MARK: - Layout tokens

    func testContentKeepsTheFortyPointBottomInset() {
        XCTAssertEqual(DS.Metric.contentBottomInset, 40)
        XCTAssertEqual(DS.Metric.runtimeStatusBottomInset, 8)
        XCTAssertEqual(DS.Metric.runtimeStatusTrailingInset, 12)
    }

    func testSearchFieldKeepsItsFinalizedGeometry() {
        XCTAssertEqual(DS.Metric.searchFieldWidth, 210)
        XCTAssertEqual(DS.Metric.searchFieldCompactWidth, 150)
        XCTAssertEqual(DS.Metric.searchFieldHeight, 28)
        XCTAssertEqual(DS.Metric.contentHeaderHeight, 48)
        XCTAssertLessThan(DS.Metric.searchFieldCompactWidth, DS.Metric.searchFieldWidth)
        // Both widths must fit the narrowest content column without clipping.
        XCTAssertLessThan(DS.Metric.searchFieldWidth + 16, 260)
    }

    // MARK: - Stage badge

    func testStageBadgeKeepsItsCompactMetrics() {
        XCTAssertEqual(StageBadge.Metrics.height, 16)
        XCTAssertEqual(StageBadge.Metrics.horizontalPadding, 6)
        XCTAssertEqual(StageBadge.Metrics.textSize, 8)
    }

    func testStageBadgeToneFollowsTheRowStatus() {
        XCTAssertEqual(StageBadge.Tone.forStatus(.blocked), .warn)
        XCTAssertEqual(StageBadge.Tone.forStatus(.failed), .error)
        for status in [MonitorStatus.running, .completed, .cancelled, .stale, .unknown] {
            XCTAssertEqual(StageBadge.Tone.forStatus(status), .neutral)
        }
    }

    // MARK: - Row typography

    func testRowTrailingMetadataIsNinePoint() {
        XCTAssertEqual(DS.Font.rowTrailingSize, 9)
        XCTAssertLessThan(DS.Font.rowTrailingSize, 11,
                          "the trailing column must stay quieter than the row's own meta text")
    }

    // MARK: - Sidebar counts

    func testSidebarCountSitsEighteenPointsInsideEveryRow() {
        XCTAssertEqual(DS.Metric.sidebarCountTrailingInset, 18)
        XCTAssertLessThan(DS.Metric.sidebarCountTrailingInset, DS.Metric.sidebarWidth / 2,
                          "the inset must leave room for the label at every sidebar width")
    }

    func testSidebarCountTintIsOneSharedRuleForEveryView() {
        XCTAssertEqual(MonitorView.blocked.countTint?.rawValue, "blocked")
        XCTAssertEqual(MonitorView.failed.countTint?.rawValue, "failed")
        XCTAssertNil(MonitorView.active.countTint)
        XCTAssertNil(MonitorView.recent.countTint)
        XCTAssertNil(MonitorView.completed.countTint)
        XCTAssertEqual(MonitorView.allCases.count, 5, "no view may sit outside the shared rule")
    }

    // MARK: - Attention without a row edge

    func testAttentionSemanticsSurviveWithoutTheRowEdge() {
        XCTAssertTrue(MonitorStatus.blocked.isAttention)
        XCTAssertTrue(MonitorStatus.failed.isAttention)
        XCTAssertFalse(MonitorStatus.running.isAttention)
        XCTAssertFalse(MonitorStatus.stale.isAttention)
        XCTAssertFalse(MonitorStatus.completed.isAttention)
    }
}
