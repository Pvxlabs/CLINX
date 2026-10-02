import AppKit
import SwiftUI

/// Level 2 — the execution list. Header, grouped rows, pagination.
struct ExecutionListView: View {
    @ObservedObject var store: MonitorStore
    @FocusState private var listFocused: Bool
    @State private var filterOpen = false
    @State private var archiveOpen = false

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
                .font(.system(size: 13, weight: .medium))
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

            if !store.currentArchives.isEmpty {
                ToolbarIconButton(system: "archivebox", help: "Archived on this Mac (\(store.currentArchives.count))",
                                  active: archiveOpen) { archiveOpen.toggle() }
                    .popover(isPresented: $archiveOpen, arrowEdge: .bottom) {
                        LocalArchiveView(store: store)
                    }
            }
            ToolbarIconButton(system: "line.3.horizontal.decrease", size: 13, help: "Filter",
                              active: filterOpen || store.projectFilter != nil || store.hostFilter != nil) {
                filterOpen.toggle()
            }
            .popover(isPresented: $filterOpen, arrowEdge: .bottom) {
                FilterPopover(store: store)
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
        ForEach(tasks, id: \.taskRef) { task in
            ExecutionRowView(store: store,
                             task: task,
                             status: store.status(of: task),
                             dimmed: store.isStaleSnapshot) {
                store.beginSelection(task.taskRef)
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
                .font(.system(size: 12))
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
    private var selected: Bool { store.selectedRef == task.taskRef }

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
                        .frame(minWidth: status.isAttention ? 58 : nil, alignment: .trailing)
                        .opacity(hovering && status.isAttention ? 0 : 1)
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
                    .fixedSize(horizontal: true, vertical: false)

                    StageBadge(stage: task.stage, tone: StageBadge.Tone.forStatus(status))

                    Spacer(minLength: 4)
                    Text(task.rowTail(status: status))
                        .font(DS.Font.rowTrailing)
                        .monospacedDigit()
                        .lineLimit(1)
                        .foregroundStyle(tailColor)
                }
                .padding(.leading, 20)
            }
            .padding(.horizontal, 16)
            .frame(height: DS.Metric.rowHeight)
            .background(selected ? DS.Palette.selection : (hovering ? DS.Palette.hover : .clear))
            // No status accent strip: the row reads its state from the status glyph, the
            // stage chip and the trailing label, never from a 2pt edge.
            .opacity(dimmed && status == .stale ? 0.8 : 1)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(alignment: .topTrailing) {
            if hovering && status.isAttention {
                Button { store.archiveLocally(task) } label: {
                    Image(systemName: "archivebox")
                        .font(.system(size: 12))
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
            if status.isAttention {
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
        VStack(alignment: .leading, spacing: 4) {
            Text("Filter executions")
                .font(DS.Font.metaEmphasis)
                .foregroundStyle(DS.Palette.textPrimary)
                .padding(.bottom, 4)

            row("Project") {
                chip("All", on: store.projectFilter == nil) { store.projectFilter = nil }
                ForEach(store.projectOptions, id: \.name) { option in
                    chip(option.name, on: store.projectFilter == option.name) { store.projectFilter = option.name }
                }
            }
            row("Host") {
                chip("All", on: store.hostFilter == nil) { store.hostFilter = nil }
                ForEach(store.hostOptions, id: \.name) { option in
                    chip(option.name, on: store.hostFilter == option.name) { store.hostFilter = option.name }
                }
            }
            row("State") {
                ForEach(MonitorView.allCases) { view in
                    chip(view.label, on: store.view == view) { store.view = view }
                }
            }
            row("Time") {
                ForEach(TimeWindow.allCases) { window in
                    chip(window.rawValue, on: store.timeWindow == window) { store.timeWindow = window }
                }
            }
        }
        .padding(12)
        .frame(width: 280, alignment: .leading)
    }

    private func row<Content: View>(_ key: String, @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(key)
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textTertiary)
                .frame(width: 64, alignment: .leading)
            FlowRow(spacing: 4) { content() }
        }
        .padding(.vertical, 2)
    }

    private func chip(_ label: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(DS.Font.meta)
                .foregroundStyle(on ? DS.Palette.accent : DS.Palette.textSecondary)
                .padding(.horizontal, 6)
                .frame(height: 20)
                .background(RoundedRectangle(cornerRadius: 4)
                    .fill(on ? DS.Palette.selection : .clear))
                .overlay(RoundedRectangle(cornerRadius: 4)
                    .strokeBorder(on ? DS.Palette.accent : DS.Palette.border, lineWidth: 1))
        }
        .buttonStyle(.plain)
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
