import AppKit
import SwiftUI

struct ActivityView: View, Equatable {
    let task: ObservedTask
    let service: (any ActivityServing)?

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.task.taskRef == rhs.task.taskRef && lhs.task.executionRef == rhs.task.executionRef &&
        lhs.task.hostOperations == rhs.task.hostOperations &&
        lhs.task.hostOperationsHasMore == rhs.task.hostOperationsHasMore &&
        lhs.task.exactResult == rhs.task.exactResult &&
        lhs.task.monitorStatus.rawValue == rhs.task.monitorStatus.rawValue &&
        lhs.task.state == rhs.task.state &&
        lhs.task.executionState == rhs.task.executionState &&
        lhs.task.codexRunning == rhs.task.codexRunning &&
        lhs.task.timestamps.completedAt == rhs.task.timestamps.completedAt &&
        lhs.task.timestamps.observedAt == rhs.task.timestamps.observedAt
    }

    var body: some View {
        // Cache only parent snapshot inputs. The live feed owns a separate observation
        // boundary so message publications cannot be skipped by this equality check.
        ActivityContentView(task: task, service: service)
    }
}

/// Converts the high-frequency AppKit live-scroll stream into the two states the UI needs.
/// `nil` means the notification was a duplicate and must not invalidate SwiftUI state.
struct ActivityScrollTransition {
    private(set) var last: Bool? = nil

    mutating func update(_ atBottom: Bool) -> Bool? {
        guard last != atBottom else { return nil }
        last = atBottom
        return atBottom
    }
}

private struct ActivityContentView: View {
    let task: ObservedTask
    @StateObject private var feed: ActivityStore
    @State private var following = true
    @State private var newActivity = false
    @State private var loadingEarlier = false
    @State private var scrollScheduled = false

    init(task: ObservedTask, service: (any ActivityServing)?) {
        self.task = task
        _feed = StateObject(wrappedValue: ActivityStore(taskRef: task.taskRef,
            executionRef: task.executionRef ?? "", service: service,
            terminal: Self.isTerminal(task)))
    }

    private static func isTerminal(_ task: ObservedTask) -> Bool {
        let terminalStates = ["COMPLETED", "FAILED", "CANCELLED", "STOPPED"]
        if task.timestamps.completedAt != nil || terminalStates.contains(task.state) ||
            terminalStates.contains(task.executionState) {
            return true
        }
        // A result is terminal only when it carries a terminal outcome. A blocked or
        // retry-required presentation state alone can still represent active work.
        guard let resultStatus = task.exactResult?.status else { return false }
        guard ["completed", "failed", "cancelled"].contains(task.monitorStatus.rawValue) else { return false }
        return ["PASS", "FAILED", "CANCELLED"].contains(resultStatus)
    }

    private var operations: [HostOperation] {
        task.hostOperations.filter { $0.executionRef == task.executionRef }
            .sorted { ($0.startedAt ?? "") < ($1.startedAt ?? "") }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "text.bubble")
                Text("Execution activity").font(DS.Font.metaEmphasis)
                Spacer()
                ActivitySyncLabel(status: feed.syncStatus)
            }
            .foregroundStyle(DS.Palette.textSecondary)
            .padding(.horizontal, 24).padding(.vertical, 12)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 20) {
                        if let notice = feed.notice {
                            Text(notice).font(DS.Font.meta).foregroundStyle(DS.Palette.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if feed.olderCursor != nil {
                            Button("Load earlier feedback") {
                                following = false
                                loadingEarlier = true
                                let anchor = feed.messages.first?.id
                                Task {
                                    await feed.refresh(older: true)
                                    if let anchor { proxy.scrollTo(anchor, anchor: .top) }
                                    DispatchQueue.main.async { loadingEarlier = false }
                                }
                            }
                            .buttonStyle(.plain).foregroundStyle(DS.Palette.accent)
                            .disabled(feed.loading || feed.reachedLimit)
                        }
                        if feed.messages.isEmpty && feed.notice == nil {
                            Text(feed.loading ? "Loading execution feedback…" : "No feedback has been recorded for this execution yet.")
                                .font(DS.Font.meta).foregroundStyle(DS.Palette.textTertiary)
                        }
                        ForEach(feed.messages) { message in
                            ActivityMessageView(message: message).equatable().id(message.id)
                        }
                        if !operations.isEmpty {
                            DisclosureGroup {
                                VStack(alignment: .leading, spacing: 12) {
                                    ForEach(operations) { operation in
                                        HStack(alignment: .top, spacing: 8) {
                                            Image(systemName: operation.completedAt == nil ? "terminal" :
                                                (operation.exitCode == 0 ? "checkmark.circle" : "exclamationmark.circle"))
                                                .foregroundStyle(DS.Palette.textTertiary)
                                            VStack(alignment: .leading, spacing: 3) {
                                                Text(operation.operation).font(DS.Font.metaEmphasis)
                                                Text("\(operation.resultState) · \(operation.host)" +
                                                     (operation.exitCode.map { " · exit \($0)" } ?? ""))
                                                    .font(DS.Font.micro).foregroundStyle(DS.Palette.textSecondary)
                                            }
                                            Spacer()
                                            Text(RelativeTime.clock(TimestampParser.date(from: operation.startedAt)))
                                                .font(DS.Font.mono(10)).foregroundStyle(DS.Palette.textTertiary)
                                        }
                                    }
                                    if task.hostOperationsHasMore {
                                        Text("Showing the latest 20 tool activities.")
                                            .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
                                    }
                                }.padding(.top, 8)
                            } label: {
                                Text("Tool activity · \(operations.count)").font(DS.Font.metaEmphasis)
                            }
                        }
                        if let result = task.exactResult {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Execution result · \(result.status)").font(DS.Font.metaEmphasis)
                                if let summary = result.summary, !summary.isEmpty {
                                    Text(summary).font(DS.Font.meta).textSelection(.enabled)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        Text("Recorded feedback · execution status is shown above")
                            .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
                        Color.clear.frame(height: 1).id("activity-bottom")
                    }
                    .padding(.horizontal, 24).padding(.top, 8).padding(.bottom, 24)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(ActivityScrollObserver { atBottom in
                        if following != atBottom { following = atBottom }
                        if atBottom { newActivity = false }
                    })
                }
                .overlay(alignment: .bottomTrailing) {
                    if !following {
                        Button {
                            following = true; newActivity = false
                            scrollScheduled = false
                            proxy.scrollTo("activity-bottom", anchor: .bottom)
                        } label: {
                            Label(newActivity ? "New activity" : "Jump to latest", systemImage: "arrow.down")
                                .font(DS.Font.meta)
                        }
                        .buttonStyle(.bordered).padding(16)
                    }
                }
                .onChange(of: feed.messages) { _ in
                    guard !loadingEarlier else { return }
                    followUpdates(proxy)
                }
                .onChange(of: operations.map { "\($0.id):\($0.resultState):\($0.completedAt ?? "")" }) { _ in
                    followUpdates(proxy)
                }
                .onChange(of: task.exactResult?.receivedAt) { _ in followUpdates(proxy) }
            }
        }
        .task { await feed.run() }
        .onChange(of: task.monitorStatus) { _ in
            feed.updateTerminal(Self.isTerminal(task))
        }
        .onDisappear { feed.stop() }
    }

    private func followUpdates(_ proxy: ScrollViewProxy) {
        if following {
            guard !scrollScheduled else { return }
            scrollScheduled = true
            DispatchQueue.main.async {
                if following { proxy.scrollTo("activity-bottom", anchor: .bottom) }
                scrollScheduled = false
            }
        } else { newActivity = true }
    }
}

private struct ActivityMessageView: View, Equatable {
    let message: ActivityMessage
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 8) {
                Text(message.kind == "result" ? "Codex · Final response" : "Codex")
                    .font(DS.Font.metaEmphasis)
                Text(RelativeTime.clock(message.date))
                    .font(DS.Font.mono(10)).foregroundStyle(DS.Palette.textTertiary)
            }
            Text(message.formattedText)
                .font(.system(size: 12.5)).foregroundStyle(DS.Palette.textPrimary)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            if message.truncated {
                Text("Message shortened to 16 KB").font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ActivitySyncLabel: View {
    @ObservedObject var status: ActivitySyncStatus
    var body: some View {
        Text(status.lastFetch.map { "Synced \(RelativeTime.clock($0)) · ~2s" } ?? "Waiting for activity")
            .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
    }
}

/// Observe user scrolling without changing NSScrollView's delegate or handling input.
/// Publish only a bottom/not-bottom transition, never every pixel of scrolling.
private struct ActivityScrollObserver: NSViewRepresentable {
    let changed: (Bool) -> Void
    func makeNSView(context: Context) -> Probe { Probe() }
    func updateNSView(_ nsView: Probe, context: Context) { nsView.changed = changed }
    static func dismantleNSView(_ nsView: Probe, coordinator: ()) { nsView.disconnect() }

    final class Probe: NSView {
        var changed: ((Bool) -> Void)?
        private var observer: NSObjectProtocol?
        private var transition = ActivityScrollTransition()
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            disconnect()
            guard window != nil else { return }
            DispatchQueue.main.async { [weak self] in self?.connect() }
        }
        private func connect() {
            guard observer == nil, let scroll = enclosingScrollView else { return }
            observer = NotificationCenter.default.addObserver(forName: NSScrollView.didLiveScrollNotification,
                object: scroll, queue: .main) { [weak self, weak scroll] _ in
                    guard let self, let scroll, let document = scroll.documentView else { return }
                    let bottom = document.bounds.maxY - scroll.contentView.bounds.maxY < 40
                    if let value = self.transition.update(bottom) { self.changed?(value) }
                }
        }
        func disconnect() {
            if let observer { NotificationCenter.default.removeObserver(observer) }
            observer = nil
            transition = ActivityScrollTransition()
        }
        deinit { disconnect() }
    }
}
