import AppKit
import SwiftUI

/// Three-pane desktop shell: Sidebar · List · Inspector, under one compact header.
///
/// Figma source of truth: the hand-tuned `MonitorWindow` renders in
/// `CLINX Monitor UI/UX Redesign` (`OgzTpC5hnbctXciUVN6wri`), page `5:3841` (Live / Healthy).
/// The header is a single 38pt bar — native traffic lights and the
/// tool buttons all sit on that one level; there is no separate macOS toolbar or search row.
struct MonitorRootView: View {
    @ObservedObject var store: MonitorStore
    @State private var windowWidth: CGFloat = 1440
    @State private var sidebarExpanded: Bool?
    @State private var navigationOpen = false
    @State private var navigationButtonHovered = false
    @State private var navigationDrawerHovered = false
    @State private var navigationCloseTask: Task<Void, Never>?
    @State private var historyOpen = false
    @State private var listPopover: ListPopover?
    @Environment(\.displayScale) private var displayScale
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Wide windows start with a docked sidebar; compact windows use the overlay drawer.
    private var sidebarDocked: Bool { sidebarExpanded ?? (windowWidth >= 1280) }
    private var listWidth: CGFloat {
        if windowWidth < 1000 { return DS.Metric.listWidthNarrow }
        if windowWidth < 1280 { return DS.Metric.listWidthMedium }
        return DS.Metric.listWidthWide
    }
    private var sidebarWidth: CGFloat { sidebarDocked ? DS.Metric.sidebarWidth : 8 }
    private var inspectorWidth: CGFloat { max(0, windowWidth - sidebarWidth - listWidth) }
    private var inspectorStacked: Bool { inspectorWidth < DS.Metric.inspectorStackThreshold }
    private var borderWidth: CGFloat { 1 / max(displayScale, 1) }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .top) {
                VStack(spacing: 0) {
                    header
                    HStack(spacing: 0) {
                        Group {
                            if sidebarDocked {
                                SidebarView(store: store, rail: false)
                            } else {
                                Color.clear
                            }
                        }
                            .frame(width: sidebarWidth)
                            .captureGeometry("sidebar")
                            .background(DS.Palette.canvas)

                        HStack(spacing: 0) {
                            ExecutionListView(store: store, popover: $listPopover)
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
            .overlay(alignment: .topLeading) {
                if historyOpen {
                    ZStack(alignment: .topLeading) {
                        Color.clear.contentShape(Rectangle())
                            .onTapGesture { historyOpen = false }
                        NavigationHistoryView(store: store) { historyOpen = false }
                            .frame(width: min(520, max(280, proxy.size.width - 140)))
                            .background(DS.Palette.surface)
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                            .overlay(RoundedRectangle(cornerRadius: 12)
                                .strokeBorder(DS.Palette.border, lineWidth: borderWidth))
                            .shadow(color: .black.opacity(0.12), radius: 12, y: 5)
                            .padding(.top, DS.Metric.contentHeaderHeight)
                            .padding(.leading, 124)
                    }
                    .ignoresSafeArea()
                }
            }
            .overlay(alignment: .topLeading) {
                if let popover = listPopover {
                    ZStack(alignment: .topLeading) {
                        Color.clear.contentShape(Rectangle())
                            .onTapGesture { listPopover = nil }
                        Group {
                            switch popover {
                            case .archive: LocalArchiveView(store: store)
                            case .options: FilterPopover(store: store)
                            }
                        }
                        .background(DS.Palette.surface)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12)
                            .strokeBorder(DS.Palette.border, lineWidth: borderWidth))
                        .shadow(color: .black.opacity(0.12), radius: 12, y: 5)
                        .padding(.leading, max(12, sidebarWidth + listWidth - 16
                            - (popover == .archive ? 36 + 360 : 330)))
                        .padding(.top, DS.Metric.contentHeaderHeight + DS.Metric.listHeaderHeight - 2)
                    }
                    .ignoresSafeArea()
                    .background {
                        Button("Close list options", action: { listPopover = nil })
                            .keyboardShortcut(.cancelAction)
                            .hidden()
                            .accessibilityHidden(true)
                    }
                }
            }
            // Environment + connection live here now: one transparent, read-only label in the
            // window's bottom-right corner rather than a capsule in the toolbar.
            .overlay(alignment: .bottomTrailing) {
                RuntimeStatus(state: store.runtimeStatus)
                    .help([store.connectivityNote, store.connectivityDetail, store.connectivityTail]
                        .compactMap { $0 }.joined(separator: " · "))
                    .padding(.bottom, DS.Metric.runtimeStatusBottomInset)
                    .padding(.trailing, DS.Metric.runtimeStatusTrailingInset)
            }
            .overlay(alignment: .topLeading) {
                ZStack(alignment: .topLeading) {
                    if navigationOpen {
                        Color.black.opacity(0.24)
                            .allowsHitTesting(false)
                            .transition(.opacity)
                        Color.clear.contentShape(Rectangle())
                            .onTapGesture { navigationOpen = false }
                            .padding(.top, DS.Metric.contentHeaderHeight)
                        VStack(spacing: 4) {
                            HStack {
                                Text("Navigation").font(DS.Font.bodyEmphasis)
                                Spacer()
                            }
                            .frame(height: 26)
                            .padding(.horizontal, 18)
                            .padding(.top, 10)
                            SidebarView(store: store, rail: false) { navigationOpen = false }
                        }
                        .frame(width: DS.Metric.sidebarWidth + 32)
                        .background(DS.Palette.canvas)
                        .clipShape(PanelShape(radius: 12, corners: [.topRight, .bottomRight]))
                        .shadow(color: .black.opacity(0.12), radius: 12, x: 4)
                        .onHover { hovering in
                            navigationDrawerHovered = hovering
                            updateNavigationHover()
                        }
                        .padding(.top, DS.Metric.contentHeaderHeight)
                        .transition(reduceMotion ? .opacity : .move(edge: .leading))
                        .zIndex(1)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .clipped()
                .ignoresSafeArea()
                .allowsHitTesting(navigationOpen)
                // Animate only the floating navigation, never the list or inspector layout.
                .animation(reduceMotion ? .easeOut(duration: 0.12) : .easeInOut(duration: 0.24),
                           value: navigationOpen)
                .background {
                    if navigationOpen {
                        Button("Close navigation", action: { navigationOpen = false })
                            .keyboardShortcut(.cancelAction)
                            .hidden()
                            .accessibilityHidden(true)
                    }
                }
            }
            .onAppear { windowWidth = proxy.size.width }
            .onChange(of: proxy.size.width) { windowWidth = $0 }
            .onDisappear { navigationCloseTask?.cancel() }
            .onChange(of: listPopover) { if $0 != nil { historyOpen = false; navigationOpen = false } }
            .onChange(of: historyOpen) { if $0 { listPopover = nil } }
            .onChange(of: navigationOpen) { if $0 { listPopover = nil } }
        }
        .background(FullSizeContentConfigurator())
        .onExitCommand {
            if listPopover != nil {
                listPopover = nil
            } else if navigationOpen {
                navigationOpen = false
            } else if historyOpen {
                historyOpen = false
            } else if store.selectedRef != nil {
                store.clearSelection()
            }
        }
    }

    // MARK: header

    private func updateNavigationHover() {
        navigationCloseTask?.cancel()
        if navigationButtonHovered || navigationDrawerHovered {
            if !sidebarDocked {
                historyOpen = false
                navigationOpen = true
            }
        } else {
            // Bridge the small gap between the toolbar button and the drawer without flicker.
            navigationCloseTask = Task { @MainActor in
                do { try await Task.sleep(nanoseconds: 180_000_000) } catch { return }
                if !navigationButtonHovered && !navigationDrawerHovered { navigationOpen = false }
            }
        }
    }

    /// One compact header with navigation following the native traffic lights.
    private var header: some View {
        HStack(spacing: 0) {
            WindowDragRegion().frame(width: 90)
            HStack(spacing: 6) {
                ToolbarIconButton(system: "sidebar.left", size: 14,
                                  help: sidebarDocked ? "Collapse sidebar" : "Expand sidebar",
                                  active: navigationOpen) {
                    navigationCloseTask?.cancel()
                    historyOpen = false
                    sidebarExpanded = !sidebarDocked
                    navigationOpen = false
                }
                .accessibilityLabel(sidebarDocked ? "Collapse sidebar" : "Expand sidebar")
                .onHover { hovering in
                    navigationButtonHovered = hovering
                    updateNavigationHover()
                }
                ToolbarIconButton(system: "clock", size: 14, help: "Opened history", active: historyOpen) {
                    historyOpen.toggle()
                }
                .accessibilityLabel("Opened history")
                ToolbarIconButton(system: "chevron.left", help: "Back  ⌘[") { store.goBack() }
                    .disabled(!store.canGoBack)
                    .opacity(store.canGoBack ? 1 : 0.35)
                    .accessibilityLabel("Back")
                    .keyboardShortcut("[", modifiers: .command)
                ToolbarIconButton(system: "chevron.right", help: "Forward  ⌘]") { store.goForward() }
                    .disabled(!store.canGoForward)
                    .opacity(store.canGoForward ? 1 : 0.35)
                    .accessibilityLabel("Forward")
                    .keyboardShortcut("]", modifiers: .command)
            }
            .padding(.trailing, 8)
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
