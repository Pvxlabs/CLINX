import SwiftUI

/// Native sessions have their own observation identity and may have no CLINX execution.
struct ObservationActivityView: View {
    let observationID: String
    @StateObject private var feed: NetworkObservationStore

    init(observationID: String, service: (any NetworkObservationServing)?) {
        self.observationID = observationID
        _feed = StateObject(wrappedValue: NetworkObservationStore(client: service))
    }

    private var response: String? {
        feed.context?.lastCodexResult ?? feed.turns.first?.summary
    }

    // Repeated collection snapshots are one visible update. Different turn/state/text
    // evidence retains its original identity and chronological order.
    private var updates: [ObservationActivityEntry] {
        Self.distinctUpdates(feed.activity)
    }

    static func distinctUpdates(_ activity: [ObservationActivityEntry]) -> [ObservationActivityEntry] {
        var result: [ObservationActivityEntry] = []
        var seen = Set<[String?]>()
        for entry in activity.reversed() where entry.text?.isEmpty == false {
            if seen.insert([entry.turnId, entry.executionRef, entry.nativeState,
                            entry.businessResult, entry.kind, entry.text]).inserted {
                result.append(entry)
            }
        }
        return result
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "text.bubble")
                Text("Session activity").font(DS.Font.metaEmphasis)
                Spacer()
                Button("Refresh") {
                    Task { await feed.select(observationID); await feed.loadContext() }
                }
                .buttonStyle(.plain)
            }
            .foregroundStyle(DS.Palette.textSecondary)
            .padding(.horizontal, 24).padding(.vertical, 12)
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let error = feed.error {
                        Text(error).foregroundStyle(DS.Palette.textSecondary)
                    }
                    if let response, !response.isEmpty {
                        VStack(alignment: .leading, spacing: 7) {
                            Text("Codex · Session response").font(DS.Font.metaEmphasis)
                            Text(response).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    } else {
                        Text(feed.selectedID == nil ? "Loading session feedback…" :
                             "No assistant feedback is available in the received session history.")
                            .foregroundStyle(DS.Palette.textSecondary)
                    }
                    if feed.context?.nextCursor != nil {
                        Button("Load earlier session response") { Task { await feed.loadContext(older: true) } }
                            .buttonStyle(.plain).foregroundStyle(DS.Palette.accent)
                    }
                    if !updates.isEmpty {
                        Text("Recorded updates").font(DS.Font.metaEmphasis)
                        ForEach(updates) { entry in
                            VStack(alignment: .leading, spacing: 7) {
                                Text("\(entry.nativeState) · \(RelativeTime.clock(Date(timeIntervalSince1970: entry.recordedAt)))")
                                    .font(DS.Font.micro).foregroundStyle(DS.Palette.textSecondary)
                                Text(entry.text ?? "").textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    if feed.activityCursor != nil {
                        Button("Load earlier updates") { Task { await feed.loadActivity() } }
                            .buttonStyle(.plain).foregroundStyle(DS.Palette.accent)
                    }
                    if feed.context?.contextStatus == "UNCACHED_CONTENT_UNAVAILABLE" {
                        Text("Source text is unavailable. Showing received session records.")
                            .foregroundStyle(DS.Palette.textSecondary)
                    }
                    Text("Read-only session history · status is shown above")
                        .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
                }
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textPrimary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 24).padding(.top, 8).padding(.bottom, 24)
            }
        }
        .task {
            await feed.select(observationID)
            await feed.loadContext()
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 12_000_000_000) } catch { return }
                if !feed.pausedHistoryRefresh {
                    await feed.refreshSelected()
                    await feed.loadContext()
                }
            }
        }
    }
}
