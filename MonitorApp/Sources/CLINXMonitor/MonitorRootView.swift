import AppKit
import SwiftUI

/// Three-pane desktop shell: Sidebar · List · Inspector, under one 48pt header.
///
/// Figma source of truth: the hand-tuned `MonitorWindow` renders in
/// `CLINX Monitor UI/UX Redesign` (`OgzTpC5hnbctXciUVN6wri`), page `5:3841` (Live / Healthy).
/// The header is a single 48pt bar — native traffic lights, the search field and the two
/// tool buttons all sit on that one level; there is no separate macOS toolbar or search row.
struct MonitorRootView: View {
    @ObservedObject var store: MonitorStore
    @FocusState private var searchFocused: Bool
    @State private var windowWidth: CGFloat = 1440

    /// Breakpoints follow the design's three reference sizes: 1440 desktop (full sidebar +
    /// 384 list), 1100 compact (rail + 340 list), 900 compact (rail + 300 list).
    private var rail: Bool { windowWidth < 1280 }
    private var listWidth: CGFloat {
        if windowWidth < 1000 { return DS.Metric.listWidthNarrow }
        if windowWidth < 1280 { return DS.Metric.listWidthMedium }
        return DS.Metric.listWidthWide
    }
    private var sidebarWidth: CGFloat { rail ? DS.Metric.sidebarRailWidth : DS.Metric.sidebarWidth }
    private var searchFieldWidth: CGFloat {
        windowWidth < 1000 ? DS.Metric.searchFieldCompactWidth : DS.Metric.searchFieldWidth
    }
    private var inspectorWidth: CGFloat { max(0, windowWidth - sidebarWidth - listWidth) }
    private var inspectorStacked: Bool { inspectorWidth < DS.Metric.inspectorStackThreshold }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .top) {
                VStack(spacing: 0) {
                    if let note = store.connectivityNote {
                        ConnectivityStrip(store: store, note: note)
                    }
                    header
                    HStack(spacing: 0) {
                        SidebarView(store: store, rail: rail)
                            .frame(width: sidebarWidth)
                            .background(DS.Palette.canvas)

                        ExecutionListView(store: store)
                            .frame(width: listWidth)
                            .clipShape(PanelShape(radius: DS.Metric.panelRadius, corners: [.topLeft, .bottomLeft]))
                            .overlay(PanelShape(radius: DS.Metric.panelRadius, corners: [.topLeft, .bottomLeft])
                                .strokeBorder(DS.Palette.border, lineWidth: 1))
                            // The panels stop 40pt above the window bottom in every layout, so the
                            // RuntimeStatus dock stays clear of content — with or without a
                            // connectivity strip above, and at any window height.
                            .padding(.bottom, DS.Metric.contentBottomInset)
                            .frame(maxHeight: .infinity)
                            .background(DS.Palette.canvas)

                        InspectorView(store: store, stacked: inspectorStacked)
                            .frame(maxWidth: .infinity)
                            .clipShape(PanelShape(radius: DS.Metric.panelRadius, corners: [.topRight, .bottomRight]))
                            .overlay(PanelShape(radius: DS.Metric.panelRadius, corners: [.topRight, .bottomRight])
                                .strokeBorder(DS.Palette.border, lineWidth: 1))
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
                    .padding(.bottom, DS.Metric.runtimeStatusBottomInset)
                    .padding(.trailing, DS.Metric.runtimeStatusTrailingInset)
            }
            .onAppear { windowWidth = proxy.size.width }
            .onChange(of: proxy.size.width) { windowWidth = $0 }
        }
        .background(FullSizeContentConfigurator())
        .onChange(of: store.searchFocusRequest) { _ in searchFocused = true }
        .onExitCommand {
            if searchFocused {
                store.searchText = ""
                searchFocused = false
            } else if store.selectedRef != nil {
                store.clearSelection()
            }
        }
    }

    // MARK: header

    /// One 48pt header, exactly as in the design: native traffic lights (drawn by macOS over
    /// the leading inset), the search field aligned with the list column's leading edge, and
    /// the refresh/settings tool buttons on the right. No product title, no app mark.
    private var header: some View {
        HStack(spacing: 0) {
            // Reserve the sidebar column (plus its 1pt hairline) so the search field's left
            // edge lands on the list column, at every sidebar width — desktop column or rail.
            Spacer().frame(width: sidebarWidth + 1)
            SearchField(store: store, focused: $searchFocused, width: searchFieldWidth)
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                ToolbarIconButton(system: "arrow.clockwise", help: "Refresh  ⌘R") {
                    Task { await store.refresh() }
                }
                ToolbarIconButton(system: "gearshape", help: "Settings  ⌘,") {
                    SettingsOpener.open()
                }
            }
            .padding(.trailing, 12)
        }
        .frame(height: DS.Metric.contentHeaderHeight)
        .background(DS.Palette.canvas)
    }
}

// MARK: - Chrome pieces

struct SearchField: View {
    @ObservedObject var store: MonitorStore
    var focused: FocusState<Bool>.Binding
    let width: CGFloat

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 11))
                .foregroundStyle(DS.Palette.textTertiary)
            TextField("Search executions", text: $store.searchText)
                .textFieldStyle(.plain)
                .font(DS.Font.body)
                .focused(focused)
            if store.searchText.isEmpty {
                Kbd(text: "⌘K")
            } else {
                Button {
                    store.searchText = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(DS.Palette.textTertiary)
                }
                .buttonStyle(.plain)
                .help("Clear search")
            }
        }
        .padding(.horizontal, 8)
        .frame(width: width, height: DS.Metric.searchFieldHeight)
        .background(RoundedRectangle(cornerRadius: 7).fill(DS.Palette.surface))
        .overlay(RoundedRectangle(cornerRadius: 7)
            .strokeBorder(focused.wrappedValue ? DS.Palette.accent : DS.Palette.border, lineWidth: 1))
    }
}

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
        let topLeft = corners.contains(.topLeft) ? radius : 0
        let topRight = corners.contains(.topRight) ? radius : 0
        let bottomRight = corners.contains(.bottomRight) ? radius : 0
        let bottomLeft = corners.contains(.bottomLeft) ? radius : 0

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

/// Makes the window content extend under the native traffic lights, so the 48pt header is
/// the window's top edge (the Figma `Header` carries the traffic lights, not a separate
/// title bar). `.hiddenTitleBar` alone leaves a transparent title-bar band above the content;
/// `.fullSizeContentView` removes it.
struct FullSizeContentConfigurator: NSViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSView {
        let view = WindowBackingView()
        view.coordinator = context.coordinator
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        context.coordinator.configure(nsView.window)
    }

    final class Coordinator {
        func configure(_ window: NSWindow?) {
            guard let window else { return }
            // Defer past SwiftUI's own window-style application, which otherwise resets the
            // style mask after this view appears.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak window] in
                guard let window else { return }
                window.styleMask.insert(.fullSizeContentView)
                window.titlebarAppearsTransparent = true
                window.titleVisibility = .hidden
                window.isMovableByWindowBackground = true
                Self.alignTrafficLights(window)
            }
        }

        /// Centers the native traffic lights on the 48pt header's vertical midline so they
        /// share one optical line with the search field and the toolbar buttons.
        static func alignTrafficLights(_ window: NSWindow) {
            // The glyph inside each standard button sits ~1.25pt above the button frame's
            // midline, so the frame is centered slightly lower than the 24pt header midline.
            let targetCenterFromTop = DS.Metric.contentHeaderHeight / 2 + 1.5
            let types: [NSWindow.ButtonType] = [.closeButton, .miniaturizeButton, .zoomButton]
            let buttons = types.compactMap { window.standardWindowButton($0) }
            guard let superview = buttons.first?.superview else { return }
            let superHeight = superview.bounds.height
            for button in buttons {
                var frame = button.frame
                let currentCenterFromTop = superHeight - frame.midY
                frame.origin.y -= (targetCenterFromTop - currentCenterFromTop)
                button.frame = frame
            }
        }
    }
}

private final class WindowBackingView: NSView {
    weak var coordinator: FullSizeContentConfigurator.Coordinator?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        coordinator?.configure(window)
    }
}
