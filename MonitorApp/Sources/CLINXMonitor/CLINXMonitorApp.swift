import SwiftUI

@main
struct CLINXMonitorApp: App {
    @StateObject private var store = MonitorStore()

    var body: some Scene {
        MenuBarExtra {
            MonitorRootView(store: store)
                .frame(minWidth: 700, minHeight: 570)
                .onAppear { store.start() }
                .onDisappear { store.stop() }
        } label: {
            Label("CLINX Monitor", systemImage: store.connection == .connected ?
                  (store.aggregate == .blocked ? "exclamationmark.circle" : "circle.fill") :
                  "wifi.slash")
            .accessibilityLabel("CLINX Monitor, \(store.aggregate.rawValue), \(store.connection.label)")
        }
        .menuBarExtraStyle(.window)
    }
}

extension ConnectionState {
    var label: String {
        switch self {
        case .setup: return "Set up HTTPS endpoint and credential"
        case .loading: return "Loading"
        case .connected: return "Connected"
        case .degraded: return "Observer degraded"
        case .stale: return "Stale snapshot"
        case .error(let category): return category
        }
    }
}

private enum TaskTab: String, CaseIterable, Identifiable {
    case active = "Active", recent = "Recent"
    var id: String { rawValue }
}

struct MonitorRootView: View {
    @ObservedObject var store: MonitorStore
    @State private var tab: TaskTab = .active
    @State private var showingSettings = false

    private var tasks: [ObservedTask] { tab == .active ? store.active : store.recent }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: store.connection == .connected ? "checkmark.circle" : "exclamationmark.circle")
                VStack(alignment: .leading, spacing: 2) {
                    Text(store.connection.label).font(.headline)
                    Text(store.endpointText.isEmpty ? "No endpoint" : store.endpointText)
                        .font(.caption).lineLimit(1).textSelection(.enabled)
                    if let observed = store.lastObservedAt {
                        Text("Observed: \(observed)").font(.caption2).foregroundStyle(.secondary)
                    }
                    if let fetched = store.lastSuccessfulFetch {
                        Text("Last fetched: \(fetched.formatted(date: .omitted, time: .standard))")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if store.isRefreshing { ProgressView().controlSize(.small).accessibilityLabel("Refreshing") }
                Button("Refresh") { Task { await store.refresh() } }
                Button("Settings") { showingSettings = true }
            }
            .padding()
            Divider()
            if store.connection == .stale || store.connection.isError || store.connection == .degraded {
                Text("Last known snapshot only. Authority liveness: \(store.health?.authorityLiveness ?? "UNKNOWN").")
                    .font(.caption).foregroundStyle(.orange).frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal).padding(.top, 8)
            }
            HStack(spacing: 0) {
                VStack(spacing: 0) {
                    Picker("Tasks", selection: $tab) {
                        ForEach(TaskTab.allCases) { value in Text(value.rawValue).tag(value) }
                    }.pickerStyle(.segmented).padding()
                    if tasks.isEmpty {
                        VStack(spacing: 8) {
                            Text(store.connection == .loading ? "Loading tasks…" : "No \(tab.rawValue.lowercased()) tasks")
                            if store.connection == .setup { Text("Open Settings to connect.") }
                            if store.connection.isError { Text("The last snapshot may be stale.") }
                        }
                        .foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 0) {
                                ForEach(tasks) { task in
                                    Button { Task { await store.select(task.taskRef) } } label: {
                                        TaskRow(task: task)
                                    }
                                    .buttonStyle(.plain)
                                    Divider()
                                }
                                if (tab == .active ? store.activeNextOffset : store.recentNextOffset) != nil {
                                    Button("Load more tasks") { Task { await store.loadMore(active: tab == .active) } }
                                        .padding()
                                }
                            }
                        }
                    }
                }
                .frame(width: 270)
                Divider()
                if let detail = store.selected {
                    TaskDetailView(task: detail, store: store)
                } else {
                    VStack {
                        Text("Select a task for execution detail and event evidence")
                        if let error = store.errorCategory { Text(error).foregroundStyle(.orange) }
                    }
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .sheet(isPresented: $showingSettings) { MonitorSettingsView(store: store) }
    }
}

private extension ConnectionState {
    var isError: Bool {
        if case .error = self { return true }
        return false
    }
}

private struct TaskRow: View {
    let task: ObservedTask
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(task.displayState.rawValue).font(.caption).bold()
                    .foregroundStyle(task.displayState == .passed ? Color.green :
                                     task.displayState == .running ? Color.blue : Color.orange)
                Spacer()
                Text(task.state).font(.caption2).foregroundStyle(.secondary)
            }
            Text(task.primaryLabel).font(.body).lineLimit(2)
            Text("\(task.project ?? "Unknown project") • \(task.host ?? "Unknown host")")
                .font(.caption).foregroundStyle(.secondary)
            Text("Execution: \(task.executionRef ?? "UNKNOWN")")
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            Text(task.blocker?.message ?? task.stage).font(.caption).lineLimit(2)
            Text(task.progressLabel).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(10)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

private struct TaskDetailView: View {
    let task: ObservedTask
    @ObservedObject var store: MonitorStore
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(task.primaryLabel).font(.title3).bold()
                Text("\(task.displayState.rawValue) • \(task.state)").font(.headline)
                if task.displayState == .unknown { Text("Unknown authority state; no success inferred.").foregroundStyle(.orange) }
                if let blocker = task.blocker { GroupBox("Blocker") { Text("\(blocker.code): \(blocker.message)") } }
                GroupBox("Current stage and progress") {
                    VStack(alignment: .leading) {
                        Text("Stage: \(task.stage)")
                        Text("Activity: \(task.currentActivity.label)")
                        Text(task.progressLabel)
                        if let value = task.progressPercent { ProgressView(value: value, total: 100) }
                        Text("Phases: \(task.phases.isEmpty ? "Unknown / no canonical phase evidence" : String(task.phases.count))")
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Execution detail") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Execution: \(task.executionRef ?? "UNKNOWN")")
                        Text("State / stage: \(task.executionState) / \(task.executionStage)")
                        Text("Model: \(task.model.resolved ?? task.model.logical ?? "UNKNOWN")")
                        Text("Reasoning effort: \(task.reasoning ?? "UNKNOWN")")
                        Text("Elapsed: \(task.elapsedSeconds.map { "\($0)s" } ?? "UNKNOWN")")
                        Text("Retry required: \(task.retryRequired ? "Yes" : "No")")
                        Text("Persisted running flag: \(task.codexRunning ? "Yes" : "No")")
                        if let result = task.finalResult {
                            Text("Result: \(result.status)").bold()
                            if let summary = result.summary { Text(summary) }
                            if let validation = result.validation { Text("Validation: \(validation)") }
                            if let blockers = result.blockers { Text("Blockers: \(blockers)") }
                        } else { Text("No exact structured result") }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Event evidence") {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Coverage: \(store.eventCoverage)").foregroundStyle(.secondary)
                        if store.events.isEmpty { Text("No available event evidence") }
                        ForEach(store.events) { event in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(event.kind).bold()
                                Text("\(event.occurredAt ?? "Time unknown") • \(event.executionRef ?? "Execution unknown")")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Divider()
                        }
                        if store.eventHasMore {
                            Button("Load more events") { Task { await store.loadMoreEvents() } }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                if !task.hostOperations.isEmpty {
                    GroupBox("Host operation evidence") {
                        VStack(alignment: .leading) {
                            ForEach(task.hostOperations) { op in
                                Text("\(op.operation): \(op.resultState) • \(op.host)")
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                Text("Artifacts: \(task.artifactsStatus) • Authority liveness: \(store.health?.authorityLiveness ?? "UNKNOWN")")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding()
        }
    }
}

private struct MonitorSettingsView: View {
    @ObservedObject var store: MonitorStore
    @Environment(\.dismiss) private var dismiss
    @State private var endpoint = ""
    @State private var credential = ""
    @State private var errorText: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Observer connection").font(.title2)
            TextField("Private HTTPS endpoint", text: $endpoint)
                .textFieldStyle(.roundedBorder)
            SecureField("Observer bearer credential (leave blank to keep current)", text: $credential)
                .textFieldStyle(.roundedBorder)
            Text("The endpoint is stored locally. The credential is stored only in this Mac’s Keychain.")
                .font(.caption).foregroundStyle(.secondary)
            if let errorText { Text(errorText).foregroundStyle(.red) }
            HStack {
                Button("Save") {
                    do {
                        guard let url = URL(string: endpoint), url.scheme == "https" else {
                            throw MonitorError.invalidEndpoint
                        }
                        if !credential.isEmpty { try ObserverKeychain.replace(credential) }
                        credential = ""
                        try store.configure(endpoint: endpoint)
                        dismiss()
                    } catch {
                        credential = ""
                        errorText = "Could not save the endpoint or credential."
                    }
                }
                Button("Remove local credential") {
                    do { try ObserverKeychain.revokeLocal(); credential = ""; store.credentialsChanged() }
                    catch { errorText = "Could not remove the local credential." }
                }
                Button("Cancel") { credential = ""; dismiss() }
            }
        }
        .padding().frame(width: 490)
        .onAppear { endpoint = store.endpointText }
    }
}
