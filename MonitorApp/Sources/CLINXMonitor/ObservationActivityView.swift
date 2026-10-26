import SwiftUI

/// Native sessions have their own observation identity and may have no CLINX execution.
struct ObservationActivityView: View {
    let observationID: String
    @StateObject private var feed: NetworkObservationStore
    @State private var updatesExpanded = false

    init(observationID: String, service: (any NetworkObservationServing)?) {
        self.observationID = observationID
        _feed = StateObject(wrappedValue: NetworkObservationStore(client: service))
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
                LazyVStack(alignment: .leading, spacing: 24) {
                    if let error = feed.error {
                        Text(error).foregroundStyle(DS.Palette.textSecondary)
                    }
                    if let response = feed.renderedHistory.response {
                        VStack(alignment: .leading, spacing: 14) {
                            Text("Codex · Session response").font(DS.Font.metaEmphasis)
                            ActivityMarkdownView(text: response.text, prepared: response.markdown).equatable()
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
                    if !feed.renderedHistory.updates.isEmpty || feed.activityCursor != nil {
                        Rectangle().fill(DS.Palette.border).frame(height: 1)
                        Button {
                            updatesExpanded.toggle()
                        } label: {
                            Label("Recorded updates · \(feed.renderedHistory.updates.count)",
                                  systemImage: updatesExpanded ? "chevron.down" : "chevron.right")
                                .font(DS.Font.metaEmphasis)
                        }
                        .buttonStyle(.plain)
                        .accessibilityValue(updatesExpanded ? "Expanded" : "Collapsed")
                        if updatesExpanded {
                            // Direct lazy children: a nested eager stack would lay out
                            // every Markdown table even when it is off screen.
                            ForEach(feed.renderedHistory.updates) { entry in
                                RecordedObservationUpdateView(entry: entry).equatable()
                            }
                            if feed.activityCursor != nil {
                                Button("Load earlier updates") { Task { await feed.loadActivity() } }
                                    .buttonStyle(.plain).foregroundStyle(DS.Palette.accent)
                            }
                        }
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
                .frame(maxWidth: ActivityMarkdownView.readingWidth, alignment: .leading)
                .padding(.horizontal, 32).padding(.top, 16).padding(.bottom, 32)
                .frame(maxWidth: .infinity)
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

struct RecordedObservationUpdateView: View, Equatable {
    let entry: RenderedObservationUpdate
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("\(entry.state) · \(RelativeTime.clock(Date(timeIntervalSince1970: entry.recordedAt)))")
                .font(DS.Font.micro).foregroundStyle(DS.Palette.textSecondary)
            ActivityMarkdownView(text: entry.content.text, prepared: entry.content.markdown).equatable()
            Rectangle().fill(DS.Palette.border).frame(height: 1)
        }
    }
}
