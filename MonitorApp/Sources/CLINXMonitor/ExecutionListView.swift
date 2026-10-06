import AppKit
import SwiftUI

enum ListPopover { case archive, options }

/// Level 2 — the execution list. Header, grouped rows, pagination.
struct ExecutionListView: View {
    @ObservedObject var store: MonitorStore
    @FocusState private var listFocused: Bool
    @Binding var popover: ListPopover?

    var body: some View {
        VStack(spacing: 0) {
            header
            content
            pagination
        }
        .background(DS.Palette.surface)
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 8) {
            Text(store.view.label)
                .interfaceFont(size: 13, weight: .medium)
                .foregroundStyle(DS.Palette.textPrimary)
            Text("\(store.visibleTasks.count)")
                .font(DS.Font.meta)
                .monospacedDigit()
                .foregroundStyle(DS.Palette.textTertiary)
            FreshnessTag(freshness: store.freshness)
            Spacer(minLength: 8)

            if let project = store.projectFilter {
                Button {
                    store.projectFilter = nil
                } label: {
                    HStack(spacing: 4) {
                        Text(project).font(DS.Font.micro)
                        Image(systemName: "xmark").font(.system(size: 8))
                    }
                    .foregroundStyle(DS.Palette.accent)
                    .padding(.horizontal, 6)
                    .frame(height: 18)
                    .background(RoundedRectangle(cornerRadius: 4).fill(DS.Palette.selection))
                }
                .buttonStyle(.plain)
                .help("Clear project filter")
            }
            if let host = store.hostFilter {
                Button {
                    store.hostFilter = nil
                } label: {
                    HStack(spacing: 4) {
                        Text(host).font(DS.Font.mono(10))
                        Image(systemName: "xmark").font(.system(size: 8))
                    }
                    .foregroundStyle(DS.Palette.accent)
                    .padding(.horizontal, 6)
                    .frame(height: 18)
                    .background(RoundedRectangle(cornerRadius: 4).fill(DS.Palette.selection))
                }
                .buttonStyle(.plain)
                .help("Clear host filter")
            }

            ToolbarIconButton(system: "archivebox", help: "Archived on this Mac (\(store.currentArchives.count))",
                              active: popover == .archive) {
                popover = popover == .archive ? nil : .archive
            }
            ToolbarIconButton(system: "line.3.horizontal.decrease", size: 13, help: "Filter and sort",
                              active: popover == .options || store.projectFilter != nil || store.hostFilter != nil
                                || store.timeWindow != .day || store.executionOrder != .activity) {
                popover = popover == .options ? nil : .options
            }
        }
        .padding(.horizontal, 16)
        .frame(height: DS.Metric.listHeaderHeight)
        .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
    }

    // MARK: body

    @ViewBuilder private var content: some View {
        if store.visibleTasks.isEmpty {
            emptyState
        } else {
            ScrollView {
                LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                    ForEach(store.groupedTasks) { group in
                        if let title = group.title {
                            Section {
                                rows(group.tasks)
                            } header: {
                                groupHeader(title, count: group.tasks.count)
                            }
                        } else {
                            rows(group.tasks)
                        }
                    }
                }
            }
            .focusable()
            .focused($listFocused)
            .onMoveCommand { direction in
                switch direction {
                case .up: store.moveSelection(by: -1)
                case .down: store.moveSelection(by: 1)
                default: break
                }
            }
            .onAppear { listFocused = true }
        }
    }

    private func rows(_ tasks: [ObservedTask]) -> some View {
        ForEach(tasks, id: \.executionIdentity) { task in
            ExecutionRowView(store: store,
                             task: task,
                             status: store.status(of: task),
                             dimmed: store.isStaleSnapshot) {
                store.beginSelection(task.taskRef, executionRef: task.executionRef)
                listFocused = true
            }
        }
    }

    private func groupHeader(_ title: String, count: Int) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(DS.Font.bodyEmphasis)
                .foregroundStyle(DS.Palette.textSecondary)
            Text("\(count)")
                .font(DS.Font.body)
                .monospacedDigit()
                .foregroundStyle(DS.Palette.textSecondary)
            Spacer()
        }
        .padding(.horizontal, 16)
        .frame(height: DS.Metric.listGroupHeaderHeight)
        .background(DS.Palette.surfaceSecondary.opacity(0.92))
        .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Image(systemName: "tray")
                .font(.system(size: 15))
                .foregroundStyle(DS.Palette.textTertiary)
                .frame(width: 34, height: 34)
                .background(RoundedRectangle(cornerRadius: 8).strokeBorder(DS.Palette.border, lineWidth: 1))
            Text(store.searchText.isEmpty ? "Nothing in this view" : "No executions match “\(store.searchText)”")
                .interfaceFont(size: 12)
                .foregroundStyle(DS.Palette.textTertiary)
                .multilineTextAlignment(.center)
            Spacer()
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var pagination: some View {
        HStack {
            Text(store.visibleTasks.isEmpty ? "0 results" : "1–\(store.visibleTasks.count) of \(store.visibleTasks.count)")
                .font(DS.Font.micro)
                .monospacedDigit()
                .foregroundStyle(DS.Palette.textTertiary)
            Spacer()
            if store.hasMoreInCurrentView {
                Button("Load more") { Task { await store.loadMore() } }
                    .buttonStyle(.plain)
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.accent)
                    .help("Load the next page from the Observer")
            } else {
                Text("1 / 1")
                    .font(DS.Font.micro)
                    .monospacedDigit()
                    .foregroundStyle(DS.Palette.textTertiary)
            }
        }
        .padding(.horizontal, 12)
        .frame(height: DS.Metric.paginationHeight)
        .overlay(alignment: .top) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
    }
}

// MARK: - Row

struct ExecutionRowView: View {
    @ObservedObject var store: MonitorStore
    let task: ObservedTask
    let status: MonitorStatus
    let dimmed: Bool
    let onSelect: () -> Void

    @State private var hovering = false

    // Read selection from the observed store, including when SwiftUI reuses a lazy row.
    private var selected: Bool {
        store.selectedRef == task.taskRef && store.selectedExecutionRef == task.executionRef
    }

    var body: some View {
        Button(action: onSelect) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    StatusGlyphView(glyph: status.glyph, color: status.color, size: 12)
                        .frame(width: 12)
                    Text(task.titleText)
                        .font(DS.Font.rowTitle)
                        .foregroundStyle(DS.Palette.textPrimary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .help(task.titleText)
                    Spacer(minLength: 6)
                    Text(task.durationText)
                        .font(DS.Font.meta)
                        .monospacedDigit()
                        .foregroundStyle(DS.Palette.textTertiary)
                        .frame(width: 58, alignment: .trailing)
                        .opacity(hovering && status.canArchiveLocally ? 0 : 1)
                }
                HStack(spacing: 8) {
                    HStack(spacing: 2) {
                        Text(task.projectText)
                            .font(DS.Font.metaEmphasis)
                            .foregroundStyle(DS.Palette.textSecondary)
                        Text("·")
                            .font(DS.Font.meta)
                            .foregroundStyle(DS.Palette.textTertiary)
                        Text(task.hostText)
                            .font(DS.Font.monoRowMeta)
                            .foregroundStyle(DS.Palette.textSecondary)
                    }
                    .lineLimit(1)
                    .truncationMode(.middle)

                    StageBadge(stage: task.stage, tone: StageBadge.Tone.forStatus(status))
                        .layoutPriority(1)

                    Spacer(minLength: 4)
                    Text(task.rowTail(status: status))
                        .font(DS.Font.rowTrailing)
                        .monospacedDigit()
                        .lineLimit(1)
                        .foregroundStyle(tailColor)
                        .frame(width: 84, alignment: .trailing)
                }
                .padding(.leading, 20)
            }
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: DS.Metric.rowHeight)
            .background(selected ? DS.Palette.selection : (hovering ? DS.Palette.hover : .clear))
            // No status accent strip: the row reads its state from the status glyph, the
            // stage chip and the trailing label, never from a 2pt edge.
            .opacity(dimmed && status == .stale ? 0.8 : 1)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(alignment: .topTrailing) {
            if hovering && status.canArchiveLocally {
                Button { store.archiveLocally(task) } label: {
                    Image(systemName: "archivebox")
                        .interfaceFont(size: 12)
                        .foregroundStyle(DS.Palette.textTertiary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Archive on this Mac. Restore from the archive button above the list.")
                .accessibilityLabel("Archive \(task.titleText) on this Mac")
                .padding(.trailing, 16)
                .padding(.top, 4)
            }
        }
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Copy title") { copy(task.titleText) }
            Button("Copy execution ID") { copy(task.executionText) }
            Button("Copy summary") { copy(summaryText) }
            if status.canArchiveLocally {
                Divider()
                Button("Archive on this Mac") { store.archiveLocally(task) }
            }
            Divider()
            Text("Read-only — no execution actions")
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(status.label), \(task.titleText), \(task.projectText) on \(task.hostText)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var tailColor: Color {
        if status.isAttention { return status.color }
        if status == .stale { return DS.Palette.stale }
        return DS.Palette.textTertiary
    }

    private var summaryText: String {
        var lines = ["\(status.label) · \(task.titleText)",
                     "\(task.projectText) · \(task.hostText) · \(task.executionText)",
                     "stage \(task.stage) · last progress \(task.lastProgressText)"]
        if let blocker = task.blocker { lines.append("\(blocker.code): \(blocker.message)") }
        return lines.joined(separator: "\n")
    }

    private func copy(_ value: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(value, forType: .string)
    }
}

// MARK: - Filter

struct FilterPopover: View {
    @ObservedObject var store: MonitorStore

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("View options").font(DS.Font.bodyEmphasis)
                Spacer()
                Button("Reset") {
                    store.projectFilter = nil
                    store.hostFilter = nil
                    store.timeWindow = .day
                    store.executionOrder = .activity
                }
                .buttonStyle(.plain)
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.accent)
            }
            .padding(16)
            Divider().overlay(DS.Palette.divider)
            VStack(spacing: 12) {
                option("Project", icon: "folder", value: store.projectFilter ?? "All projects") {
                    Button("All projects") { store.projectFilter = nil }
                    ForEach(store.projectOptions) { option in
                        Button(option.name) { store.projectFilter = option.name }
                    }
                }
                option("Host", icon: "desktopcomputer", value: store.hostFilter ?? "All hosts") {
                    Button("All hosts") { store.hostFilter = nil }
                    ForEach(store.hostOptions) { option in
                        Button(option.name) { store.hostFilter = option.name }
                    }
                }
                option("State", icon: "circle.dotted", value: store.view.label) {
                    ForEach(MonitorView.allCases) { view in
                        Button(view.label) { store.view = view }
                    }
                }
            }
            .padding(16)
            Divider().overlay(DS.Palette.divider)
            VStack(spacing: 12) {
                option("Ordering", icon: "arrow.up.arrow.down", value: store.executionOrder.rawValue) {
                    ForEach(ExecutionOrder.allCases) { order in
                        Button(order.rawValue) { store.executionOrder = order }
                    }
                }
                option("Time range", icon: "clock", value: timeLabel(store.timeWindow)) {
                    ForEach(TimeWindow.allCases) { window in
                        Button(timeLabel(window)) { store.timeWindow = window }
                    }
                }
            }
            .padding(16)
        }
        .foregroundStyle(DS.Palette.textPrimary)
        .frame(width: 330)
        .background(DS.Palette.surface)
    }

    private func timeLabel(_ window: TimeWindow) -> String {
        switch window {
        case .hour: return "Past hour"
        case .day: return "Past 24 hours"
        case .week: return "Past 7 days"
        }
    }

    private func option<Content: View>(_ title: String, icon: String, value: String,
                                      @ViewBuilder content: () -> Content) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13))
                .frame(width: 16)
                .foregroundStyle(DS.Palette.textSecondary)
            Text(title).font(DS.Font.body)
                .foregroundStyle(DS.Palette.textSecondary)
            Spacer(minLength: 8)
            Menu(content: content) {
                HStack(spacing: 6) {
                    Text(value).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.down").font(.system(size: 9, weight: .medium))
                }
                .font(DS.Font.body)
                .padding(.horizontal, 10)
                .frame(width: 150, height: 28)
                .background(RoundedRectangle(cornerRadius: 7).fill(DS.Palette.surface))
                .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(DS.Palette.border, lineWidth: 1))
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .menuIndicator(.hidden)
            .fixedSize()
            .accessibilityLabel(title)
            .accessibilityValue(value)
        }
    }
}

/// Minimal wrapping row so filter chips never clip in a narrow popover.
struct FlowRow: Layout {
    var spacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: maxWidth == .infinity ? x : maxWidth, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

// MARK: - Shared toolbar button

struct ToolbarIconButton: View {
    let system: String
    var size: CGFloat = 13
    var help: String?
    var active = false
    let action: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: system)
                .font(.system(size: size))
                .foregroundStyle(active || hovering ? DS.Palette.textPrimary : DS.Palette.textTertiary)
                .frame(width: 28, height: 26)
                .background(RoundedRectangle(cornerRadius: 6)
                    .fill(active ? DS.Palette.hover : (hovering ? DS.Palette.hover : .clear)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(help ?? "")
    }
}
