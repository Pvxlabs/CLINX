import SwiftUI
import AppKit

struct NetworkObservationView: View {
    @ObservedObject var monitor: MonitorStore
    @StateObject private var network = NetworkObservationStore()

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                Text("全部设备").font(DS.Font.bodyEmphasis)
                HStack {
                    Picker("设备", selection: $network.filters.node) {
                        Text("全部设备").tag("")
                        ForEach(network.sources) { source in
                            Text(source.displayName.isEmpty ? source.nodeId : source.displayName).tag(source.nodeId)
                        }
                    }
                    TextField("项目路径", text: $network.filters.project)
                }
                HStack {
                    Picker("来源", selection: $network.filters.kind) {
                        Text("全部来源").tag("")
                        Text("外部观察").tag("external")
                        Text("受管任务").tag("managed")
                    }
                    Picker("状态", selection: $network.filters.state) {
                        Text("全部状态").tag("")
                        Text("运行中").tag("RUNNING")
                        Text("执行结束").tag("COMPLETED")
                        Text("失败").tag("FAILED")
                    }
                }
                Button("应用筛选 / 刷新") { Task { await network.refresh() } }
                if let error = network.error { Text(error).font(.caption).foregroundStyle(.secondary) }
                List(network.items) { item in
                    Button {
                        Task { await network.select(item.id) }
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.shortTitle).font(DS.Font.bodyEmphasis).lineLimit(1)
                            Text("\(item.deviceName) · \(item.statusLabel)").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 4)
                    }
                    .buttonStyle(.plain)
                    .listRowBackground(network.selectedID == item.id ? DS.Palette.surface : Color.clear)
                    .accessibilityIdentifier(item.id)
                }
                if network.nextCursor != nil {
                    Button("加载更多任务") { Task { await network.loadMore() } }
                        .disabled(network.items.count >= 500)
                }
                ForEach(network.sources.filter { $0.coverage == "AWAITING_BOOTSTRAP" || $0.gap != nil }) { source in
                    Text("\(source.displayName)：\(source.gap ?? "等待目录采集")").font(.caption)
                }
                Text("仅已批准共享范围 · 其他 Agent 未接入").font(.caption).foregroundStyle(.secondary)
                if network.retentionEvicted > 0 {
                    Text("保留策略已淘汰 \(network.retentionEvicted) 条记录").font(.caption)
                }
            }
            .padding(16)
            .frame(minWidth: 320, idealWidth: 420, maxWidth: 480)
            Divider()
            if let detail = network.detail {
                ObservationInspector(detail: detail, network: network)
                    .id(detail.item.id)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "network").font(.largeTitle)
                    Text("选择全网任务").font(DS.Font.bodyEmphasis)
                    Text("观察与记录不需要执行权限").foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(DS.Palette.canvas)
        .task(id: monitor.endpointText + monitor.credentialAccount) {
            network.configure(endpoint: monitor.endpointText, account: monitor.credentialAccount)
            while !Task.isCancelled {
                await network.refresh()
                do { try await Task.sleep(nanoseconds: network.pollNanoseconds) } catch { break }
            }
        }
    }
}

private struct ObservationInspector: View {
    let detail: ObservationDetail
    @ObservedObject var network: NetworkObservationStore
    var body: some View {
        // Stable identity and explicit pagination. No scrollTo or count-driven
        // bottom anchoring; browsing older rounds pauses timeline replacement.
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(detail.item.shortTitle).font(.title2)
                Text("\(detail.item.deviceName) · \(detail.item.project)").foregroundStyle(.secondary)
                Text(detail.item.statusLabel).font(DS.Font.bodyEmphasis)
                Text("来源：\(detail.item.source) / \(detail.item.sourceClient)")
                Text("观测：\(detail.item.freshness) · 更新 \(Date(timeIntervalSince1970: detail.item.receivedAt).formatted())")
                Text("覆盖：\(detail.item.coverage)" + (detail.gap.map { " · \($0)" } ?? ""))
                Text("轮次：\(detail.item.turn.turnId ?? "未提供")").textSelection(.enabled)
                HStack {
                    Button("复制流转入口") {
                        let route = detail.item.control
                        var mapping: [String: String] = ["node_id": route.nodeId,
                            "thread_id": route.nativeThreadId, "entrypoint": route.entrypoint]
                        if let task = route.taskRef { mapping["task_ref"] = task }
                        if let execution = route.executionRef { mapping["execution_ref"] = execution }
                        if let data = try? JSONSerialization.data(withJSONObject: mapping, options: [.sortedKeys]),
                           let text = String(data: data, encoding: .utf8) {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(text, forType: .string)
                        }
                    }
                    Button("继续") {}.disabled(true)
                    Button("取消") {}.disabled(true)
                }
                Text(detail.item.control.enabled
                     ? "流转入口已定位；继续与取消须通过正式 CLINX 授权及单写者检查"
                     : "当前仅观察；控制未授权或目标不可用（\(detail.item.control.reason)）")
                    .font(.caption).foregroundStyle(.secondary)
                Divider()
                Text("Activity · 已接收记录").font(.headline)
                Text(network.activityCoverage).font(.caption).foregroundStyle(.secondary)
                ForEach(network.activity) { event in
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(event.turnId ?? "会话") · #\(event.sourceSeq) · \(event.nativeState)").font(.caption)
                        Text(event.text ?? "状态更新").textSelection(.enabled)
                    }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(DS.Palette.surface, in: RoundedRectangle(cornerRadius: 8))
                }
                if network.activityCursor != nil {
                    Button("加载更早 Activity") { Task { await network.loadActivity() } }.disabled(network.activity.count >= 512)
                }
                Text("轮次历史").font(.headline)
                ForEach(network.turns) { turn in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(turn.turnId ?? turn.executionRef ?? "会话元数据").font(.caption.monospaced())
                        Text("\(turn.nativeState) · \(turn.executionState ?? "未受管") · 业务结果 \(turn.businessResult ?? "未判定")")
                        if let progress = turn.progress, !turn.isTerminal { Text(progress).textSelection(.enabled) }
                        if let summary = turn.summary { Text(summary).textSelection(.enabled) }
                        ForEach(turn.artifacts, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                    }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(DS.Palette.surface, in: RoundedRectangle(cornerRadius: 8))
                }
                if network.historyCursor != nil {
                    Button("加载更早轮次") { Task { await network.loadHistory() } }.disabled(network.turns.count >= 512)
                }
                Button("按需读取源端正文") { Task { await network.loadContext() } }
                if let context = network.context {
                    Text(context.contextStatus ?? "UNKNOWN").font(.caption)
                    if let user = context.lastUserIntent { Text(user).textSelection(.enabled) }
                    if let result = context.lastCodexResult { Text(result).textSelection(.enabled) }
                    if context.nextCursor != nil {
                        Button("源端更早内容") { Task { await network.loadContext(older: true) } }
                    }
                }
            }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
