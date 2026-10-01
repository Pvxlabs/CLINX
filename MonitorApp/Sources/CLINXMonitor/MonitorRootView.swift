import AppKit
import SwiftUI

/// Three-pane desktop shell: Sidebar · List · Inspector, under one compact toolbar.
///
/// Figma source of truth: `MonitorWindow.tsx` (header, connectivity strip, sidebar/list/
/// inspector geometry) plus the "Developer Handoff" page (window behaviour, keyboard).
struct MonitorRootView: View {
    @ObservedObject var store: MonitorStore
    @FocusState private var searchFocused: Bool
    @State private var windowWidth: CGFloat = 1440

    private var rail: Bool { windowWidth < 1280 }
    private var listWidth: CGFloat {
        if windowWidth < 1000 { return DS.Metric.listWidthNarrow }
        if windowWidth < 1280 { return DS.Metric.listWidthMedium }
        return DS.Metric.listWidthWide
    }
    private var showTitle: Bool { windowWidth >= 1000 }
    private var inspectorWidth: CGFloat {
        windowWidth - listWidth - (rail ? DS.Metric.sidebarRailWidth : DS.Metric.sidebarWidth)
    }
    private var inspectorStacked: Bool { inspectorWidth < DS.Metric.inspectorStackThreshold }

    var body: some View {
        VStack(spacing: 0) {
            if let note = store.connectivityNote {
                ConnectivityStrip(store: store, note: note)
            }
            NavigationSplitView {
                SidebarView(store: store, rail: rail)
                    .background(DS.Palette.canvas)
                    .navigationSplitViewColumnWidth(min: rail ? 52 : 168,
                                                    ideal: rail ? DS.Metric.sidebarRailWidth : DS.Metric.sidebarWidth,
                                                    max: rail ? 60 : 280)
            } content: {
                ExecutionListView(store: store)
                    .clipShape(PanelShape(radius: DS.Metric.panelRadius, corners: [.topLeft, .bottomLeft]))
                    .overlay(PanelShape(radius: DS.Metric.panelRadius, corners: [.topLeft, .bottomLeft])
                        .strokeBorder(DS.Palette.border, lineWidth: 1))
                    .padding(EdgeInsets(top: 8, leading: 8, bottom: 8, trailing: 0))
                    .frame(maxHeight: .infinity)
                    .background(DS.Palette.canvas)
                    .navigationSplitViewColumnWidth(min: 260, ideal: listWidth, max: 520)
            } detail: {
                InspectorView(store: store, stacked: inspectorStacked)
                    .clipShape(PanelShape(radius: DS.Metric.panelRadius, corners: [.topRight, .bottomRight]))
                    .overlay(PanelShape(radius: DS.Metric.panelRadius, corners: [.topRight, .bottomRight])
                        .strokeBorder(DS.Palette.border, lineWidth: 1))
                    .padding(EdgeInsets(top: 8, leading: 0, bottom: 8, trailing: 8))
                    .frame(maxHeight: .infinity)
                    .background(DS.Palette.canvas)
            }
            .background(DS.Palette.canvas)
        }
        .background(DS.Palette.canvas)
        .overlay(alignment: .top) {
            if store.syntheticScenario != nil {
                Rectangle().fill(DS.Palette.synthetic).frame(height: 2)
            }
        }
        .background(
            GeometryReader { proxy in
                Color.clear
                    .onAppear { windowWidth = proxy.size.width }
                    .onChange(of: proxy.size.width) { windowWidth = $0 }
            }
        )
        .background(WindowConfigurator())
        .toolbar { toolbarContent }
        .onChange(of: store.searchFocusRequest) { _ in searchFocused = true }
        .onExitCommand {
            if searchFocused {
                store.searchText = ""
                searchFocused = false
            } else if store.selectedRef != nil {
                store.clearSelection()
            }
        }
        // The product name lives in the leading toolbar cluster (as in the design), so the
        // window title is left empty rather than repeating it in the centre of the bar.
        .navigationTitle("")
    }

    // MARK: toolbar

    @ToolbarContentBuilder private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            HStack(spacing: 10) {
                // The design renders the app mark at 24pt with a −3pt margin so the tile
                // aligns with the 18pt toolbar icons.
                AppMark(size: 24, flat: true).padding(-3)
                if showTitle {
                    Text("CLINX Monitor")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(DS.Palette.textPrimary)
                }
                EnvironmentBadge(synthetic: store.syntheticScenario != nil)
                if let scenario = store.syntheticScenario {
                    ScenarioSelector(store: store, current: scenario, compact: !showTitle)
                }
            }
        }

        ToolbarItemGroup(placement: .automatic) {
            ConnectionCluster(connection: store.connection7, authority: store.authority,
                              sync: store.syncText, compact: !showTitle)
            SearchField(store: store, focused: $searchFocused, width: showTitle ? 210 : 140)
            ToolbarIconButton(system: "arrow.clockwise", help: "Refresh  ⌘R") {
                Task { await store.refresh() }
            }
            ToolbarIconButton(system: "gearshape", help: "Settings  ⌘,") {
                SettingsOpener.open()
            }
        }
    }
}

// MARK: - Chrome pieces

struct ScenarioSelector: View {
    @ObservedObject var store: MonitorStore
    let current: SyntheticScenario
    var compact = false

    var body: some View {
        Menu {
            ForEach(SyntheticScenario.allCases) { scenario in
                Button(scenario.label) { store.useSynthetic(scenario) }
            }
            Divider()
            Button("Exit synthetic mode") { store.useLiveObserver() }
        } label: {
            HStack(spacing: 6) {
                if !compact {
                    Text("SCENARIO")
                        .font(DS.Font.mono(10))
                        .tracking(0.5)
                        .foregroundStyle(DS.Palette.synthetic.opacity(0.75))
                }
                Text(current.label)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DS.Palette.textPrimary)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 7, weight: .semibold))
                    .foregroundStyle(DS.Palette.synthetic)
            }
            .padding(.horizontal, 8)
            .frame(height: 22)
            .background(RoundedRectangle(cornerRadius: 5).fill(DS.Palette.surface))
            .overlay(RoundedRectangle(cornerRadius: 5)
                .strokeBorder(DS.Palette.synthetic.opacity(0.45), lineWidth: 1))
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Synthetic acceptance scenario (development only)")
    }
}

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
        .frame(width: width, height: 28)
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

/// Hides the centred window title: the design carries the product name in the leading
/// toolbar cluster, and the reserved title space would push the search field into the
/// toolbar's overflow menu on narrow windows.
struct WindowConfigurator: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async { view.window?.titleVisibility = .hidden }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        DispatchQueue.main.async { nsView.window?.titleVisibility = .hidden }
    }
}

/// Opens the app's Settings scene (no `SettingsLink` on macOS 13).
enum SettingsOpener {
    static func open() {
        NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
    }
}
