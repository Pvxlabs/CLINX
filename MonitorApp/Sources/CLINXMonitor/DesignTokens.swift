import AppKit
import SwiftUI

/// Central design-token layer.
///
/// Values are read from the Figma source of truth: `CLINX Monitor UI/UX Redesign`
/// (Figma Make, file `gjtQIi4778Yoh5cvRv5gwS`) — `src/index.css` (`:root` / `[data-theme='dark']`)
/// and the "Design Tokens" page of that file. Nothing here is a per-view magic number.
///
/// Typography note: the Figma file lists `Inter` with an `SF Pro` fallback for UI text and
/// `SF Mono` for technical identifiers. This app ships no web fonts, so the UI uses the
/// macOS system font (SF Pro) and every identifier/code/timestamp uses SF Mono via
/// `.monospaced` — the same substitution the design's own font stack requests on macOS.
enum DS {

    // MARK: - Color

    enum Palette {
        /// `--bg` / `--surface`
        static let surface = dynamic(0xFCFCFC, 0x0F1011)
        /// `--canvas`, also the window chrome behind the panes
        static let canvas = dynamic(0xF4F4F5, 0x08090A)
        /// `--sidebar`
        static let sidebar = dynamic(0xF4F4F5, 0x08090A)
        /// `--surface-2`
        static let surfaceSecondary = dynamic(0xF4F4F5, 0x17181A)
        /// `--border`
        static let border = dynamic(0xE4E4E7, 0x23252A)
        /// `--divider`
        static let divider = dynamic(0xEFEFF1, 0x1B1C1F)
        /// `--text-1`
        static let textPrimary = dynamic(0x1B1B1F, 0xF7F8F8)
        /// `--text-2`
        static let textSecondary = dynamic(0x62626B, 0x8A8F98)
        /// `--text-3`
        static let textTertiary = dynamic(0x8F8F98, 0x62666D)
        /// `--selection`
        static let selection = dynamic(0xECECEF, 0x1C1D21)
        /// `--selection-strong` / `--focus`
        static let accent = dynamic(0x5E6AD2, 0x7C84E8)
        /// `--hover`
        static let hover = dynamic(0xF1F1F3, 0x16171A)
        /// `--synthetic`
        static let synthetic = dynamic(0x9B59D0, 0xB07BE3)

        // Status colors
        static let running = dynamic(0x5E6AD2, 0x7C84E8)
        static let success = dynamic(0x3D9A64, 0x4CB782)
        static let warning = dynamic(0xD27B1E, 0xF2994A)
        static let error = dynamic(0xD4483E, 0xEB5757)
        static let stale = dynamic(0xB8962A, 0xD8B443)
        static let unknown = dynamic(0x9A9AA3, 0x6B6F76)
    }

    // MARK: - Metrics

    enum Metric {
        /// Figma: "Single 52px unified toolbar".
        static let toolbarHeight: CGFloat = 52
        /// Figma: "Two-line 54px row".
        static let rowHeight: CGFloat = 54
        static let listHeaderHeight: CGFloat = 40
        static let listGroupHeaderHeight: CGFloat = 32
        static let paginationHeight: CGFloat = 26
        static let connectivityStripHeight: CGFloat = 28
        static let inspectorTabsHeight: CGFloat = 34
        static let sidebarItemHeight: CGFloat = 28
        static let sidebarRailWidth: CGFloat = 52
        static let sidebarWidth: CGFloat = 200
        static let listWidthWide: CGFloat = 384
        static let listWidthMedium: CGFloat = 340
        static let listWidthNarrow: CGFloat = 300
        static let windowRadius: CGFloat = 0
        static let panelRadius: CGFloat = 10
        static let blockerRadius: CGFloat = 8
        static let controlRadius: CGFloat = 5
        static let windowMinWidth: CGFloat = 900
        static let windowMinHeight: CGFloat = 600
        static let inspectorStackThreshold: CGFloat = 760

        /// Figma: the list/inspector panels sit 40pt above the MonitorWindow bottom in
        /// every layout — desktop, Dark Mode, and Compact Window alike. A connectivity
        /// strip only moves the panels' top edge; the bottom inset is unchanged.
        static let contentBottomInset: CGFloat = 40
        /// Figma: the read-only RuntimeStatus docks inside that 40pt band, bottom-right.
        static let runtimeStatusBottomInset: CGFloat = 8
        static let runtimeStatusTrailingInset: CGFloat = 12

        /// Figma: "SearchField" is 210 × 28 above the main content, in a 48pt header row.
        /// The Compact Window pages narrow it to 150 at 900pt; 1100 and 1440 keep 210.
        static let searchFieldWidth: CGFloat = 210
        static let searchFieldCompactWidth: CGFloat = 150
        static let searchFieldHeight: CGFloat = 28
        static let contentHeaderHeight: CGFloat = 48

        /// Figma `Text:align`: the sidebar count's right edge sits 18pt inside the
        /// SidebarItem's right edge, identically for every view and every state.
        static let sidebarCountTrailingInset: CGFloat = 18
    }

    // MARK: - Typography

    enum Font {
        static let inspectorTitle = SwiftUI.Font.system(size: 18, weight: .semibold)
        static let rowTitle = SwiftUI.Font.system(size: 13, weight: .medium)
        static let body = SwiftUI.Font.system(size: 12)
        static let bodyEmphasis = SwiftUI.Font.system(size: 12, weight: .medium)
        static let meta = SwiftUI.Font.system(size: 11)
        static let metaEmphasis = SwiftUI.Font.system(size: 11, weight: .medium)
        static let micro = SwiftUI.Font.system(size: 10.5)
        static let microSemibold = SwiftUI.Font.system(size: 10.5, weight: .semibold)
        static let sectionLabel = SwiftUI.Font.system(size: 10.5, weight: .semibold)
        static let blockerTitle = SwiftUI.Font.system(size: 13.5, weight: .semibold)
        static let statValue = SwiftUI.Font.system(size: 12.5, weight: .medium)

        static func mono(_ size: CGFloat, weight: SwiftUI.Font.Weight = .regular) -> SwiftUI.Font {
            .system(size: size, weight: weight, design: .monospaced)
        }

        static let monoID = mono(11)
        static let monoRowMeta = mono(10.5)
        static let monoMicro = mono(9, weight: .semibold)

        /// Figma: TaskRow trailing metadata ("2m ago", "Blocked 6m ago") — one step below
        /// `meta`, so the right-hand column stays quieter than the row's own subject.
        static let rowTrailingSize: CGFloat = 9
        static let rowTrailing = SwiftUI.Font.system(size: rowTrailingSize)
    }
}

// MARK: - Dynamic color helper

private func dynamic(_ light: UInt32, _ dark: UInt32) -> Color {
    Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        return NSColor(srgbHex: isDark ? dark : light)
    })
}

private extension NSColor {
    convenience init(srgbHex hex: UInt32) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: 1)
    }
}
