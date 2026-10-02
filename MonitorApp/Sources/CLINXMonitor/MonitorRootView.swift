import AppKit
import SwiftUI

/// Three-pane desktop shell: Sidebar · List · Inspector, under one compact header.
///
/// Figma source of truth: the hand-tuned `MonitorWindow` renders in
/// `CLINX Monitor UI/UX Redesign` (`OgzTpC5hnbctXciUVN6wri`), page `5:3841` (Live / Healthy).
/// The header is a single 38pt bar — native traffic lights and the two
/// tool buttons all sit on that one level; there is no separate macOS toolbar or search row.
struct MonitorRootView: View {
    @ObservedObject var store: MonitorStore
    @State private var windowWidth: CGFloat = 1440
    @Environment(\.displayScale) private var displayScale

    /// Breakpoints follow the design's three reference sizes: 1440 desktop (full sidebar +
    /// 384 list), 1100 compact (rail + 340 list), 900 compact (rail + 300 list).
    private var rail: Bool { windowWidth < 1280 }
    private var listWidth: CGFloat {
        if windowWidth < 1000 { return DS.Metric.listWidthNarrow }
        if windowWidth < 1280 { return DS.Metric.listWidthMedium }
        return DS.Metric.listWidthWide
    }
    private var sidebarWidth: CGFloat { rail ? DS.Metric.sidebarRailWidth : DS.Metric.sidebarWidth }
    private var inspectorWidth: CGFloat { max(0, windowWidth - sidebarWidth - listWidth) }
    private var inspectorStacked: Bool { inspectorWidth < DS.Metric.inspectorStackThreshold }
    private var borderWidth: CGFloat { 1 / max(displayScale, 1) }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .top) {
                VStack(spacing: 0) {
                    header
                    HStack(spacing: 0) {
                        SidebarView(store: store, rail: rail)
                            .frame(width: sidebarWidth)
                            .captureGeometry("sidebar")
                            .background(DS.Palette.canvas)

                        HStack(spacing: 0) {
                            ExecutionListView(store: store)
                                .frame(width: listWidth)
                                .captureGeometry("list")
                                // Draw the shared boundary once, inside the list's existing bounds.
                                .overlay(alignment: .trailing) {
                                    Rectangle().fill(DS.Palette.border)
                                        .frame(width: borderWidth)
                                        .allowsHitTesting(false)
                                }

                            InspectorView(store: store, stacked: inspectorStacked)
                                .frame(maxWidth: .infinity)
                                .captureGeometry("inspector")
                        }
                        .clipShape(PanelShape(radius: DS.Metric.panelRadius,
                                             corners: [.topLeft, .bottomLeft, .topRight, .bottomRight]))
                        .overlay {
                            PanelShape(radius: DS.Metric.panelRadius,
                                       corners: [.topLeft, .bottomLeft, .topRight, .bottomRight])
                                .strokeBorder(DS.Palette.border, lineWidth: borderWidth)
                                .allowsHitTesting(false)
                        }
                        // One outer border and one shared separator, each one backing pixel.
                        // The panels stop 40pt above the window bottom in every layout.
                        .padding(.bottom, DS.Metric.contentBottomInset)
                        .padding(.trailing, 8)
                        .frame(maxHeight: .infinity)
                        .background(DS.Palette.canvas)
                    }
                    .frame(maxHeight: .infinity)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(DS.Palette.canvas)

                // Synthetic-mode edge anchored to the window's absolute top (y=0), drawn as a
                // ZStack sibling so it is not pushed by the safe-area inset that `.overlay` sees.
                if store.syntheticScenario != nil {
                    Rectangle().fill(DS.Palette.synthetic)
                        .frame(height: 2)
                        .frame(maxWidth: .infinity)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .ignoresSafeArea()
            // Environment + connection live here now: one transparent, read-only label in the
            // window's bottom-right corner rather than a capsule in the toolbar.
            .overlay(alignment: .bottomTrailing) {
                RuntimeStatus(state: store.runtimeStatus)
                    .help([store.connectivityNote, store.connectivityDetail, store.connectivityTail]
                        .compactMap { $0 }.joined(separator: " · "))
                    .padding(.bottom, DS.Metric.runtimeStatusBottomInset)
                    .padding(.trailing, DS.Metric.runtimeStatusTrailingInset)
            }
            .onAppear { windowWidth = proxy.size.width }
            .onChange(of: proxy.size.width) { windowWidth = $0 }
        }
        .background(FullSizeContentConfigurator())
        .onExitCommand {
            if store.selectedRef != nil {
                store.clearSelection()
            }
        }
    }

    // MARK: header

    /// One compact header: native traffic lights on the left and refresh/settings on the right.
    private var header: some View {
        HStack(spacing: 0) {
            WindowDragRegion()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            HStack(spacing: 10) {
                ToolbarIconButton(system: "arrow.clockwise", help: "Refresh  ⌘R") {
                    Task { await store.refresh() }
                }
                .captureGeometry("refresh")
                ToolbarIconButton(system: "gearshape", help: "Settings  ⌘,") {
                    SettingsOpener.open()
                }
                .captureGeometry("settings")
            }
            .padding(.trailing, 12)
        }
        .frame(height: DS.Metric.contentHeaderHeight)
        .captureGeometry("header")
        .background(DS.Palette.canvas)
    }
}

// MARK: - Chrome pieces

struct ConnectivityStrip: View {
    @ObservedObject var store: MonitorStore
    let note: String

    private var color: Color {
        switch store.connection7 {
        case .connected: return DS.Palette.success
        case .degraded: return DS.Palette.stale
        case .offline: return DS.Palette.error
        }
    }

    private var glyph: StatusGlyph {
        store.connection7 == .offline ? .squareCross : .triangle
    }

    var body: some View {
        HStack(spacing: 8) {
            StatusGlyphView(glyph: glyph, color: color, size: 11)
            Text(note)
                .font(.system(size: 11.5, weight: .semibold))
                .foregroundStyle(DS.Palette.textPrimary)
            if let detail = store.connectivityDetail {
                Text(detail)
                    .font(.system(size: 11.5))
                    .foregroundStyle(DS.Palette.textSecondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            if let tail = store.connectivityTail {
                Text(tail)
                    .font(DS.Font.mono(10))
                    .foregroundStyle(DS.Palette.textTertiary)
            }
        }
        .padding(.horizontal, 16)
        .frame(height: DS.Metric.connectivityStripHeight)
        .background(color.opacity(0.08))
        .overlay(alignment: .bottom) { Rectangle().fill(color.opacity(0.25)).frame(height: 1) }
    }
}

/// Selectively rounded corners (macOS 13 has no `UnevenRoundedRectangle`).
struct PanelShape: InsettableShape {
    enum Corner { case topLeft, topRight, bottomLeft, bottomRight }

    let radius: CGFloat
    let corners: Set<Corner>
    var inset: CGFloat = 0

    func path(in rect: CGRect) -> Path {
        let rect = rect.insetBy(dx: inset, dy: inset)
        var path = Path()
        let cornerRadius = max(0, min(radius - inset, min(rect.width, rect.height) / 2))
        let topLeft = corners.contains(.topLeft) ? cornerRadius : 0
        let topRight = corners.contains(.topRight) ? cornerRadius : 0
        let bottomRight = corners.contains(.bottomRight) ? cornerRadius : 0
        let bottomLeft = corners.contains(.bottomLeft) ? cornerRadius : 0

        path.move(to: CGPoint(x: rect.minX + topLeft, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX - topRight, y: rect.minY))
        path.addArc(center: CGPoint(x: rect.maxX - topRight, y: rect.minY + topRight),
                    radius: topRight, startAngle: .degrees(-90), endAngle: .degrees(0), clockwise: false)
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - bottomRight))
        path.addArc(center: CGPoint(x: rect.maxX - bottomRight, y: rect.maxY - bottomRight),
                    radius: bottomRight, startAngle: .degrees(0), endAngle: .degrees(90), clockwise: false)
        path.addLine(to: CGPoint(x: rect.minX + bottomLeft, y: rect.maxY))
        path.addArc(center: CGPoint(x: rect.minX + bottomLeft, y: rect.maxY - bottomLeft),
                    radius: bottomLeft, startAngle: .degrees(90), endAngle: .degrees(180), clockwise: false)
        path.addLine(to: CGPoint(x: rect.minX, y: rect.minY + topLeft))
        path.addArc(center: CGPoint(x: rect.minX + topLeft, y: rect.minY + topLeft),
                    radius: topLeft, startAngle: .degrees(180), endAngle: .degrees(270), clockwise: false)
        path.closeSubpath()
        return path
    }

    func inset(by amount: CGFloat) -> PanelShape {
        var copy = self
        copy.inset += amount
        return copy
    }
}

/// Opens the app's Settings scene (no `SettingsLink` on macOS 13).
enum SettingsOpener {
    static func open() {
        NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
    }
}
