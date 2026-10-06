import SwiftUI

/// Level 1 navigation: views, hosts and the chain.
///
/// Used by the docked sidebar and the temporary navigation drawer.
struct SidebarView: View {
    @ObservedObject var store: MonitorStore
    let rail: Bool
    var onNavigate: () -> Void = {}

    var body: some View {
        let counts = store.counts
        VStack(alignment: .leading, spacing: 0) {
            if !rail {
                Text("Executions")
                    .interfaceFont(size: 11.5, weight: .medium)
                    .foregroundStyle(DS.Palette.textTertiary)
                    .padding(.horizontal, 8)
                    .padding(.top, 4)
                    .padding(.bottom, 4)
            } else {
                Spacer().frame(height: 6)
            }

            ForEach(Array(MonitorView.allCases.enumerated()), id: \.element.id) { index, view in
                if index == 3 {
                    Rectangle()
                        .fill(DS.Palette.divider)
                        .frame(width: rail ? 24 : nil, height: 1)
                        .padding(.vertical, 6)
                        .padding(.horizontal, rail ? 0 : 8)
                }
                SidebarItemView(store: store, view: view, rail: rail, count: counts[view] ?? 0, onNavigate: onNavigate)
            }

            if !rail {
                if !store.hostOptions.isEmpty {
                    Text("Hosts")
                        .interfaceFont(size: 11.5, weight: .medium)
                        .foregroundStyle(DS.Palette.textTertiary)
                        .padding(.horizontal, 8)
                        .padding(.top, 16)
                        .padding(.bottom, 4)

                    ForEach(store.hostOptions) { host in
                        HostRow(store: store, host: host, onNavigate: onNavigate)
                    }
                }

                Text("Chain")
                    .interfaceFont(size: 11.5, weight: .medium)
                    .foregroundStyle(DS.Palette.textTertiary)
                    .padding(.horizontal, 8)
                    .padding(.top, 16)
                    .padding(.bottom, 4)

                ChainRows(store: store)

                Spacer(minLength: 12)

                HStack(spacing: 6) {
                    Image(systemName: "eye").font(.system(size: 11))
                    Text(store.syntheticScenario == nil ? "Read-only observer" : "Fixture · read-only")
                        .font(DS.Font.micro)
                }
                .foregroundStyle(DS.Palette.textTertiary)
                .padding(.horizontal, 10)
                .padding(.bottom, 10)
                .padding(.top, 8)
                .overlay(alignment: .top) {
                    Rectangle().fill(DS.Palette.divider).frame(height: 1).padding(.horizontal, 8)
                }
            } else {
                Spacer()
            }
        }
        .padding(.horizontal, rail ? 7 : 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct SidebarItemView: View {
    @ObservedObject var store: MonitorStore
    let view: MonitorView
    let rail: Bool
    let count: Int
    let onNavigate: () -> Void

    @State private var hovering = false

    private var active: Bool { store.view == view }
    /// Attention colour comes from the view's shared rule, and only while the view
    /// actually holds entries — a "0" stays neutral grey on Blocked and Failed too.
    private var attention: MonitorStatus? {
        guard count > 0 else { return nil }
        return view.countTint
    }

    var body: some View {
        Button {
            store.view = view
            onNavigate()
        } label: {
            if rail {
                ZStack(alignment: .topTrailing) {
                    glyph(size: 13)
                        .frame(width: 38, height: 32)
                        .foregroundStyle(active ? DS.Palette.accent : DS.Palette.textSecondary)
                        .background(RoundedRectangle(cornerRadius: 6)
                            .fill(active ? DS.Palette.selection : (hovering ? DS.Palette.hover : .clear)))
                }
                .padding(.bottom, 2)
            } else {
                HStack(spacing: 10) {
                    glyph(size: 13)
                        .frame(width: 16)
                        .foregroundStyle(active ? DS.Palette.textPrimary : DS.Palette.textTertiary)
                    Text(view.label)
                        .interfaceFont(size: 13, weight: .medium)
                        .foregroundStyle(active ? DS.Palette.textPrimary : DS.Palette.textSecondary)
                    Spacer(minLength: 6)
                    countView
                }
                .padding(.leading, 8)
                // Every view — Active, Blocked, Failed, Recent, Completed — ends its count
                // on this one column, so "5 / 2 / 1 / 12 / 6" share a right edge.
                .padding(.trailing, DS.Metric.sidebarCountTrailingInset)
                .frame(height: DS.Metric.sidebarItemHeight)
                .background(RoundedRectangle(cornerRadius: 6)
                    .fill(active ? DS.Palette.selection : (hovering ? DS.Palette.hover : .clear)))
                .contentShape(Rectangle())
            }
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(rail ? "\(view.label) (\(count))  ⌘\(view.shortcutIndex)" : "⌘\(view.shortcutIndex)")
        .accessibilityLabel("\(view.label), \(count) executions")
    }

    /// One renderer for every view and every state. The count is plain text — no badge,
    /// no pill, no tinted fill, no minimum width and no padding — so the digits hug the
    /// shared right edge and attention only ever changes their colour.
    private var countView: some View {
        Text("\(count)")
            .font(DS.Font.metaEmphasis)
            .monospacedDigit()
            .foregroundStyle(attention?.color ?? DS.Palette.textTertiary)
    }

    @ViewBuilder private func glyph(size: CGFloat) -> some View {
        switch view {
        case .blocked: StatusGlyphView(glyph: .circleMinus, color: DS.Palette.warning, size: size)
        case .failed: StatusGlyphView(glyph: .squareCross, color: DS.Palette.error, size: size)
        case .completed: StatusGlyphView(glyph: .circleCheck, color: DS.Palette.success, size: size)
        case .active: Image(systemName: "waveform.path.ecg").font(.system(size: size))
        case .recent: Image(systemName: "clock").font(.system(size: size))
        }
    }
}

private struct HostRow: View {
    @ObservedObject var store: MonitorStore
    let host: FilterOption
    let onNavigate: () -> Void

    private var active: Bool { store.hostFilter == host.name }

    var body: some View {
        Button {
            store.hostFilter = active ? nil : host.name
            onNavigate()
        } label: {
            HStack(spacing: 8) {
                Circle().fill(DS.Palette.success).frame(width: 6, height: 6)
                Text(host.name)
                    .font(DS.Font.mono(11))
                    .foregroundStyle(DS.Palette.textSecondary)
                    .lineLimit(1)
                Spacer(minLength: 4)
                Text("\(host.count)")
                    .font(DS.Font.micro)
                    .monospacedDigit()
                    .foregroundStyle(DS.Palette.textTertiary)
            }
            .padding(.horizontal, 8)
            .frame(height: 24)
            .background(RoundedRectangle(cornerRadius: 5)
                .fill(active ? DS.Palette.selection : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(active ? "Clear host filter" : "Filter by host \(host.name)")
    }
}

private struct ChainRows: View {
    @ObservedObject var store: MonitorStore

    private struct Component: Identifiable {
        let name: String
        let status: String
        var id: String { name }
    }

    private var components: [Component] {
        guard let task = store.selected else {
            return [Component(name: "Host", status: "ok"),
                    Component(name: "Provider", status: "ok"),
                    Component(name: "Worker", status: "ok")]
        }
        return [Component(name: "Host", status: task.routing.host.status),
                Component(name: "Provider", status: task.routing.provider.status),
                Component(name: "Worker", status: task.routing.transport.status)]
    }

    var body: some View {
        ForEach(components) { component in
            let healthy = ["READY", "OK", "HEALTHY", "SUCCEEDED"].contains(component.status.uppercased())
            HStack(spacing: 8) {
                if healthy {
                    Circle().fill(DS.Palette.success).frame(width: 6, height: 6)
                } else {
                    Rectangle().fill(DS.Palette.warning).frame(width: 6, height: 6).rotationEffect(.degrees(45))
                }
                Text(component.name)
                    .font(DS.Font.body)
                    .foregroundStyle(DS.Palette.textSecondary)
                Spacer(minLength: 4)
                Text(healthy ? "ok" : component.status.lowercased())
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.textTertiary)
            }
            .padding(.horizontal, 8)
            .frame(height: 22)
        }
    }
}
