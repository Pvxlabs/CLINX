"""Source-level regression for the finalized CLINX Monitor UI details.

The macOS SwiftUI app cannot be built or rendered on the P620 host, so the rules that
live in a view body — the transparent RuntimeStatus, the removed row accent strip, the
shared sidebar count renderer — are pinned here against the Swift sources. Rules that
are values rather than view structure are pinned by `FinalizedUIRuleTests` in the app's
own XCTest target.

Figma authority: design file `OgzTpC5hnbctXciUVN6wri`, pages Live / Healthy (5:3841),
Active (5:4657), Blocked (5:5531), Failed (5:6312) and their siblings.
Later user-approved iMac adjustments take precedence for header/search/navigation:
see docs/monitor/IMAC_ACCEPTED_BASELINE_20261002.md.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

SOURCES = Path(__file__).resolve().parent / "MonitorApp" / "Sources" / "CLINXMonitor"


def swift(name: str) -> str:
    path = SOURCES / name
    assert path.is_file(), f"missing Swift source {path}"
    return path.read_text(encoding="utf-8")


def all_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(SOURCES.glob("*.swift"))}


def body(source: str, marker: str) -> str:
    """Return the brace-balanced body of the first declaration containing `marker`."""
    start = source.index(marker)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces after {marker!r}")


# --- RuntimeStatus is transparent and read-only -------------------------------------------


def test_runtime_status_draws_no_container() -> None:
    view = body(swift("MonitorComponents.swift"), "struct RuntimeStatus: View")
    assert ".background(" not in view, "RuntimeStatus must stay transparent"
    assert "strokeBorder" not in view, "RuntimeStatus must not draw a border"
    assert "Capsule" not in view, "RuntimeStatus must not become a capsule again"
    assert "Button" not in view, "RuntimeStatus is read-only: no action affordance"


def test_runtime_status_keeps_its_four_states_and_labels() -> None:
    status = swift("StatusSystem.swift")
    assert "enum RuntimeStatusState" in status
    assert 'case .synthetic: return "SYNTHETIC"' in status
    for label in ("Connected", "Stale", "Offline", "Local"):
        assert f'return "{label}"' in status


def test_environment_badge_and_connection_cluster_are_gone() -> None:
    for name, source in all_sources().items():
        for retired in ("EnvironmentBadge", "ConnectionCluster", "HatchPattern", "attentionEdgeWidth"):
            assert retired not in source, f"{retired} still referenced in {name}"


# --- Header / search ----------------------------------------------------------------------


def test_header_keeps_navigation_without_a_system_toolbar() -> None:
    root = swift("MonitorRootView.swift")
    header = body(root, "private var header: some View")
    assert "toolbarContent" not in root
    for symbol in ("sidebar.left", "clock", "chevron.left", "chevron.right", "arrow.clockwise", "gearshape"):
        assert f'ToolbarIconButton(system: "{symbol}"' in header


def test_header_keeps_the_accepted_imac_height() -> None:
    tokens = swift("DesignTokens.swift")
    assert "static let contentHeaderHeight: CGFloat = 38" in tokens
    assert ".frame(height: DS.Metric.contentHeaderHeight)" in body(swift("MonitorRootView.swift"), "private var header: some View")


def test_removed_search_row_does_not_return() -> None:
    root = swift("MonitorRootView.swift")
    assert "searchHeader" not in root
    assert "SearchField(" not in root
    assert re.search(r"ExecutionListView\s*\(\s*store:\s*store\b", root), (
        "the execution list must remain mounted even when its call gains legal optional arguments"
    )


# --- Main content bottom inset ------------------------------------------------------------


def test_main_content_bottom_inset_is_forty_in_every_pane() -> None:
    root = swift("MonitorRootView.swift")
    assert root.count(".padding(.bottom, DS.Metric.contentBottomInset)") == 1, "the shared panel owns one 40pt bottom inset"
    assert "bottom: 8," not in root, "no pane may fall back to the old 8pt inset"
    assert "static let contentBottomInset: CGFloat = 40" in swift("DesignTokens.swift")


def test_runtime_status_docks_bottom_right_inside_that_band() -> None:
    root = swift("MonitorRootView.swift")
    assert ".overlay(alignment: .bottomTrailing)" in root
    assert re.search(r"RuntimeStatus\s*\(\s*state:\s*store\.runtimeStatus\b", root), (
        "the read-only runtime status must remain docked while allowing its environment context"
    )
    tokens = swift("DesignTokens.swift")
    assert "static let runtimeStatusBottomInset: CGFloat = 8" in tokens
    assert "static let runtimeStatusTrailingInset: CGFloat = 12" in tokens


# --- TaskRow ------------------------------------------------------------------------------


def test_task_row_has_no_status_accent_strip() -> None:
    row = body(swift("ExecutionListView.swift"), "struct ExecutionRowView: View")
    assert "overlay(alignment: .leading)" not in row, "the 2pt left accent strip must stay gone"
    assert "attentionEdgeWidth" not in row
    assert "DS.Font.rowTrailing" in row, "the trailing metadata is 9pt"
    assert "StageBadge.Tone.forStatus(status)" in row


def test_row_height_token_is_untouched() -> None:
    # Deliberate adaptation recorded in docs/monitor/UI_REDESIGN.md §5.2: the principles,
    # IA and tokens pages specify 54px rows; the Make render drew 52px. 54 stays.
    assert "static let rowHeight: CGFloat = 54" in swift("DesignTokens.swift")


# --- StageBadge ---------------------------------------------------------------------------


def test_stage_badge_is_compact_and_hugs_its_text() -> None:
    components = swift("MonitorComponents.swift")
    badge = body(components, "struct StageBadge: View")
    assert "static let height: CGFloat = 16" in badge
    assert "static let horizontalPadding: CGFloat = 6" in badge
    assert "static let textSize: CGFloat = 8" in badge
    assert ".font(DS.Font.mono(Metrics.textSize))" in badge
    assert ".padding(.horizontal, Metrics.horizontalPadding)" in badge
    assert ".frame(height: Metrics.height)" in badge
    assert "minWidth" not in badge, "the badge hugs its text: no fixed or minimum width"
    assert ".frame(width:" not in badge


# --- Sidebar counts -----------------------------------------------------------------------


def test_sidebar_count_is_one_shared_renderer() -> None:
    sidebar = swift("SidebarView.swift")
    count = body(sidebar, "private var countView: some View")
    assert "minWidth" not in count, "no leftover minimum width"
    assert ".background(" not in count, "the count is plain text, never a badge or pill"
    assert ".padding(" not in count, "no padding may inset the digits"
    assert "attention?.color ?? DS.Palette.textTertiary" in count
    assert sidebar.count("private var countView") == 1, "every view shares this one renderer"


def test_sidebar_count_uses_the_shared_eighteen_point_inset() -> None:
    sidebar = swift("SidebarView.swift")
    assert "DS.Metric.sidebarCountTrailingInset" in sidebar
    assert "static let sidebarCountTrailingInset: CGFloat = 18" in swift("DesignTokens.swift")
    assert ".padding(.horizontal, 8)\n                .frame(height: DS.Metric.sidebarItemHeight)" not in sidebar


def test_sidebar_attention_colour_is_a_shared_view_rule() -> None:
    status = swift("StatusSystem.swift")
    assert "var countTint: MonitorStatus?" in status
    sidebar = swift("SidebarView.swift")
    assert "view.countTint" in sidebar, "no per-view special case for Blocked or Failed"


# --- Compact / dark / synthetic must not regress ------------------------------------------


def test_collapsed_navigation_has_no_icon_counter_rail() -> None:
    root = swift("MonitorRootView.swift")
    assert "rail: true" not in root
    assert "sidebarExpanded = !sidebarDocked" in root
    hover = body(root, "private func updateNavigationHover()")
    assert "if !sidebarDocked" in hover
    assert "navigationOpen = true" in hover


def test_dark_mode_still_resolves_through_dynamic_tokens() -> None:
    tokens = swift("DesignTokens.swift")
    assert tokens.count("dynamic(0x") >= 19, "every palette entry keeps a light/dark pair"
    assert "appearance.bestMatch(from: [.aqua, .darkAqua])" in tokens


def test_synthetic_semantics_are_still_distinct() -> None:
    synthetic = swift("SyntheticData.swift")
    assert "enum SyntheticScenario" in synthetic
    assert "observerReadOnly: true" in synthetic, "fixtures stay inside the read-only boundary"
    assert "readOnly: true" in synthetic, "the health response still reports read-only"
    root = swift("MonitorRootView.swift")
    edge = body(root, "if store.syntheticScenario != nil")
    assert "Rectangle().fill(DS.Palette.synthetic)" in edge and ".frame(height: 2)" in edge
    assert ".ignoresSafeArea()" in root
    settings = swift("MonitorSettingsView.swift")
    assert "ForEach(SyntheticScenario.allCases)" in settings
    assert "store.useSynthetic($0)" in settings


def test_row_sizes_come_from_the_token_layer_not_from_the_view() -> None:
    row = body(swift("ExecutionListView.swift"), "struct ExecutionRowView: View")
    assert "font(.system(size: 9" not in row, "the trailing size lives in DS.Font"
    assert "DS.Font.rowTrailing" in row
    assert "static let rowTrailingSize: CGFloat = 9" in swift("DesignTokens.swift")


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__, "-q"]))
