import AppKit
import SwiftUI

/// Level 3 — the inspector. Header and tabs stay fixed; the body scrolls.
struct InspectorView: View {
    @ObservedObject var store: MonitorStore
    let stacked: Bool

    @State private var tab: Tab = .inspection

    enum Tab { case inspection, raw, activity }

    var body: some View {
        Group {
            if let task = store.selected {
                content(for: task)
            } else {
                EmptyStateView(hasTasks: !store.allTasks.isEmpty)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(DS.Palette.surface)
    }

    @ViewBuilder private func content(for task: ObservedTask) -> some View {
        let status = task.monitorStatus
        VStack(spacing: 0) {
            ExecutionHeaderView(task: task, stacked: stacked)
            if store.selectedDetailIncomplete {
                Text("当前 Observer 未提供该历史 execution 的 task 详情；保留精确列表快照，未用当前 execution 替代。")
                    .font(DS.Font.meta)
                    .foregroundStyle(DS.Palette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 10)
            }
            tabBar(task)
            if tab == .activity {
                if let id = task.observationId {
                    ObservationActivityView(observationID: id, service: store.observationService)
                        .id(store.activitySource + id)
                } else {
                    ActivityView(task: task, service: store.activityService)
                        .equatable()
                        .id(store.activitySource + task.taskRef + (task.executionRef ?? ""))
                }
            } else {
                ScrollView {
                    if tab == .raw {
                        RawSnapshotView(task: task)
                    } else {
                        VStack(alignment: .leading, spacing: 20) {
                            if let blocker = task.blocker {
                                BlockerPanelView(task: task, status: status, blocker: blocker)
                            }
                            if stacked {
                                VStack(alignment: .leading, spacing: 20) {
                                    overview(task)
                                    timeline(task, status: status)
                                }
                            } else {
                                HStack(alignment: .top, spacing: 28) {
                                    overview(task).frame(minWidth: 240, maxWidth: 300, alignment: .leading)
                                    timeline(task, status: status)
                                }
                            }
                        }
                        .padding(.horizontal, 24)
                        .padding(.top, 20)
                        .padding(.bottom, 24)
                    }
                }
            }
        }
    }

    private func tabBar(_ task: ObservedTask) -> some View {
        HStack(spacing: 20) {
            tabButton("Inspection", .inspection)
            tabButton("Raw snapshot", .raw)
            tabButton("Activity", .activity)
            Spacer()
            Text("snapshot \(RelativeTime.clock(TimestampParser.date(from: task.timestamps.observedAt)))")
                .font(DS.Font.mono(10))
                .foregroundStyle(DS.Palette.textTertiary)
                .padding(.bottom, 7)
        }
        .padding(.horizontal, 24)
        .frame(height: DS.Metric.inspectorTabsHeight, alignment: .bottom)
        .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
    }

    private func tabButton(_ title: String, _ value: Tab) -> some View {
        Button {
            tab = value
        } label: {
            Text(title)
                .interfaceFont(size: 12.5, weight: .medium)
                .foregroundStyle(tab == value ? DS.Palette.textPrimary : DS.Palette.textSecondary)
                .frame(height: DS.Metric.inspectorTabsHeight)
                .overlay(alignment: .bottom) {
                    Rectangle()
                        .fill(tab == value ? DS.Palette.textPrimary : .clear)
                        .frame(height: 1)
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func overview(_ task: ObservedTask) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionLabel(title: "OVERVIEW")
            MetadataGrid(rows: task.overviewRows)
            if let uri = task.codexUri, let url = URL(string: uri),
               url.scheme == "codex", url.host == "threads" {
                HStack(spacing: 16) {
                    Link("Open in Codex", destination: url)
                    CopyableMetadataValue(label: "LINK", value: uri, displayText: "Copy link")
                }
                .font(DS.Font.body)
            }
        }
    }

    private func timeline(_ task: ObservedTask, status: MonitorStatus) -> some View {
        let items = task.timeline
        return VStack(alignment: .leading, spacing: 12) {
            SectionLabel(title: "TIMELINE", trailing: "\(items.count) events")
            if items.isEmpty {
                Text("No available event evidence")
                    .font(DS.Font.meta)
                    .foregroundStyle(DS.Palette.textTertiary)
            } else {
                TimelineView(items: items, live: status == .running)
                    .id(task.executionRef ?? task.taskRef)
            }
            HStack(spacing: 10) {
                Text("COVERAGE \(store.eventCoverage)")
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.textTertiary)
                if store.eventHasMore {
                    Button("Load more events") { Task { await store.loadMoreEvents() } }
                        .buttonStyle(.plain)
                        .font(DS.Font.micro)
                        .foregroundStyle(DS.Palette.accent)
                }
            }
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Execution header

/// Execution facts are independent of the Observer connection indicator.
struct ExecutionHeaderView: View {
    let task: ObservedTask
    let stacked: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 8) {
                Text(task.titleText)
                    .font(DS.Font.inspectorTitle)
                    .foregroundStyle(DS.Palette.textPrimary)
                    .lineLimit(2)
                    .help(task.titleText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CopyButton(text: task.titleText, label: "Copy title")
            }

            HStack(spacing: 8) {
                StatusPill(status: task.monitorStatus)
                Spacer(minLength: 0)
            }
            .padding(.top, 8)

            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), alignment: .leading),
                                     count: stacked ? 2 : 4), alignment: .leading, spacing: 14) {
                fact("HOST", task.hostText)
                fact("PROJECT", task.workspaceName, help: task.projectText)
                fact("LAST PROGRESS", task.lastProgressText)
                fact("DURATION", task.durationText)
            }
            .padding(.top, 12)
        }
        .padding(.horizontal, 24)
        .padding(.top, 20)
        .padding(.bottom, 16)
    }

    private func fact(_ label: String, _ value: String, help: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(DS.Font.micro)
                .foregroundStyle(DS.Palette.textTertiary)
            Text(value)
                .font(DS.Font.statValue)
                .monospacedDigit()
                .foregroundStyle(DS.Palette.textPrimary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
                .help(help ?? value)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 12)
        .overlay(alignment: .leading) { Rectangle().fill(DS.Palette.divider).frame(width: 1) }
    }
}

// MARK: - Blocker panel

struct BlockerPanelView: View {
    let task: ObservedTask
    let status: MonitorStatus
    let blocker: Blocker
    @State private var evidenceOpen = false

    private var failed: Bool { status == .failed }
    private var color: Color { failed ? DS.Palette.error : DS.Palette.warning }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                StatusGlyphView(glyph: failed ? .squareCross : .circleMinus, color: color, size: 14)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 8) {
                        Text(failed ? "FAILURE" : "BLOCKER")
                            .font(DS.Font.mono(10, weight: .semibold))
                            .tracking(1)
                            .foregroundStyle(color)
                        Text(blocker.code)
                            .font(DS.Font.mono(10))
                            .foregroundStyle(DS.Palette.textTertiary)
                            .textSelection(.enabled)
                    }
                    Text(failed ? "Execution failed at \(task.stage)" : "Execution blocked at \(task.stage)")
                        .font(DS.Font.blockerTitle)
                        .foregroundStyle(DS.Palette.textPrimary)
                        .padding(.top, 4)
                    Text(blocker.message)
                        .font(DS.Font.mono(11.5))
                        .foregroundStyle(DS.Palette.textSecondary)
                        .lineLimit(2)
                        .help(blocker.message)
                        .padding(.top, 2)

                    HStack(spacing: 24) {
                        fact("Code", blocker.code, mono: true)
                        fact("Last progress", task.lastProgressText, mono: false)
                        fact("Stage", task.stage, mono: true)
                    }
                    .padding(.top, 10)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)

            HStack(spacing: 4) {
                Button {
                    evidenceOpen.toggle()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.right")
                            .interfaceFont(size: 9, weight: .semibold)
                            .rotationEffect(.degrees(evidenceOpen ? 90 : 0))
                        Text(evidenceOpen ? "Hide evidence" : "Show evidence")
                            .font(DS.Font.meta)
                    }
                    .foregroundStyle(DS.Palette.textSecondary)
                    .padding(.horizontal, 6)
                    .frame(height: 20)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                CopyButton(text: "\(blocker.code): \(blocker.message)", label: "Copy details")

                Spacer()

                Text("Observe only · resolution happens in CLINX")
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.textTertiary)
                    .padding(.trailing, 6)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .overlay(alignment: .top) {
                Rectangle().fill(color.opacity(0.18)).frame(height: 1)
            }

            if evidenceOpen {
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(blocker.message)
                            .font(DS.Font.mono(10.5))
                            .foregroundStyle(DS.Palette.textSecondary)
                        Text("code: \(blocker.code)\nexecution: \(task.executionText)\nstage: \(task.stage)\nstate: \(task.state)")
                            .font(DS.Font.mono(10.5))
                            .foregroundStyle(DS.Palette.textTertiary)
                    }
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                }
                .frame(maxHeight: 180)
                .background(RoundedRectangle(cornerRadius: 4).fill(DS.Palette.surfaceSecondary))
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(DS.Palette.divider, lineWidth: 1))
                .padding(.horizontal, 10)
                .padding(.bottom, 10)
            }
        }
        .background(RoundedRectangle(cornerRadius: DS.Metric.blockerRadius).fill(color.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: DS.Metric.blockerRadius)
            .strokeBorder(color.opacity(0.30), lineWidth: 1))
    }

    private func fact(_ label: String, _ value: String, mono: Bool) -> some View {
        HStack(spacing: 6) {
            Text(label)
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textTertiary)
            Text(value)
                .font(mono ? DS.Font.mono(10.5) : DS.Font.meta)
                .monospacedDigit()
                .foregroundStyle(DS.Palette.textPrimary)
        }
    }
}

// MARK: - Timeline

struct TimelineView: View {
    let items: [TimelineItem]
    let live: Bool
    @State private var expanded = false

    static func visibleItems(_ items: [TimelineItem], expanded: Bool) -> [TimelineItem] {
        expanded ? items : Array(items.suffix(10))
    }

    var body: some View {
        let visible = Self.visibleItems(items, expanded: expanded)
        VStack(alignment: .leading, spacing: 0) {
            ForEach(visible) { item in
                TimelineRowView(item: item, last: item.id == visible.last?.id && !live)
            }
            if live {
                HStack(spacing: 12) {
                    Spacer().frame(width: 66)
                    Circle()
                        .fill(DS.Palette.running)
                        .frame(width: 6, height: 6)
                        .frame(width: 16)
                    Text("Live — awaiting next event")
                        .font(DS.Font.meta)
                        .foregroundStyle(DS.Palette.textTertiary)
                }
                .padding(.top, 2)
            }
            if items.count > 10 {
                Button(expanded ? "Show latest 10" : "Show all \(items.count) events") {
                    expanded.toggle()
                }
                .buttonStyle(.plain)
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.accent)
                .padding(.top, 8)
            }
        }
    }
}

struct TimelineRowView: View {
    let item: TimelineItem
    let last: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text(item.time)
                .font(DS.Font.mono(10.5))
                .monospacedDigit()
                .lineLimit(1)
                .fixedSize()
                .foregroundStyle(DS.Palette.textTertiary)
                .frame(width: 66, alignment: .leading)
                .padding(.top, 1)

            ZStack(alignment: .top) {
                if !last {
                    Rectangle()
                        .fill(DS.Palette.border)
                        .frame(width: 1)
                        .padding(.top, 17)
                }
                ZStack {
                    Circle().fill(DS.Palette.surface)
                    Circle().strokeBorder(item.kind == .error ? DS.Palette.error : DS.Palette.border, lineWidth: 1)
                    Image(systemName: item.kind.symbol)
                        .font(.system(size: 8, weight: .medium))
                        .foregroundStyle(item.kind.color)
                }
                .frame(width: 16, height: 16)
            }
            .frame(width: 16)
            .fixedSize(horizontal: true, vertical: false)

            VStack(alignment: .leading, spacing: 1) {
                Text(item.title)
                    .interfaceFont(size: 12, weight: item.kind == .error ? .semibold : .medium)
                    .foregroundStyle(item.kind == .error ? DS.Palette.error : DS.Palette.textPrimary)
                if item.meta != nil || item.mono != nil {
                    HStack(spacing: 8) {
                        if let meta = item.meta {
                            Text(meta).font(DS.Font.meta).monospacedDigit()
                                .foregroundStyle(DS.Palette.textSecondary)
                        }
                        if let mono = item.mono {
                            Text(mono).font(DS.Font.monoRowMeta)
                                .foregroundStyle(DS.Palette.textTertiary)
                        }
                    }
                }
            }
            .padding(.bottom, 12)

            Spacer(minLength: 0)
        }
    }
}

// MARK: - Raw snapshot

struct RawSnapshotView: View {
    let task: ObservedTask

    private var text: String {
        var payload: [String: Any] = [
            "execution_id": task.executionText,
            "task_ref": task.taskRef,
            "state": task.state,
            "stage": task.stage,
            "activity": task.activityText,
            "host": task.hostText,
            "provider": task.providerText,
            "project": task.projectText,
            "started_at": task.timestamps.startedAt ?? NSNull(),
            "last_progress_at": task.timestamps.lastProgressAt ?? NSNull(),
            "observed_at": task.timestamps.observedAt,
            "progress_basis": task.progressBasis,
            "phases": task.phases.count,
            "events": task.recentEvents.items.count,
            "event_coverage": task.recentEvents.coverage,
            "host_operations": task.hostOperations.count,
            "authority_status": task.mutationBoundary.authorityStatus,
        ]
        if let blocker = task.blocker {
            payload["blocker"] = ["code": blocker.code, "message": blocker.message]
        } else {
            payload["blocker"] = NSNull()
        }
        guard let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: [.prettyPrinted, .sortedKeys]),
              let string = String(data: data, encoding: .utf8) else { return "{}" }
        return string
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Exact observer payload — for debugging, not for reading status.")
                    .font(DS.Font.meta)
                    .foregroundStyle(DS.Palette.textTertiary)
                Spacer()
                CopyButton(text: text, label: "Copy JSON")
            }
            Text(text)
                .font(DS.Font.mono(11))
                .foregroundStyle(DS.Palette.textSecondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 5).fill(DS.Palette.surfaceSecondary))
                .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(DS.Palette.divider, lineWidth: 1))
        }
        .padding(20)
    }
}

// MARK: - Empty state

struct EmptyStateView: View {
    let hasTasks: Bool

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "tray")
                .font(.system(size: 17))
                .foregroundStyle(DS.Palette.textTertiary)
                .frame(width: 36, height: 36)
                .background(RoundedRectangle(cornerRadius: 8).strokeBorder(DS.Palette.border, lineWidth: 1))
            Text(hasTasks ? "No execution selected" : "No executions observed")
                .interfaceFont(size: 13, weight: .semibold)
                .foregroundStyle(DS.Palette.textPrimary)
            Text(hasTasks
                 ? "Select an execution to inspect its stage, blocker and timeline. Use ↑ ↓ to move through the list."
                 : "The observer is connected and healthy, but CLINX has not reported any executions in the last 24h.")
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 300)
            HStack(spacing: 4) { Kbd(text: "⌘R"); Text("Refresh") }
            .font(DS.Font.micro)
            .foregroundStyle(DS.Palette.textTertiary)
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
