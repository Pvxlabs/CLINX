import SwiftUI

struct LocalArchiveView: View {
    @ObservedObject var store: MonitorStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Archived on this Mac")
                .font(DS.Font.bodyEmphasis)
            Text("Hidden locally. Execution state on P620 is unchanged.")
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textSecondary)
            if store.currentArchives.isEmpty {
                Text("No archived executions")
                    .font(DS.Font.meta)
                    .foregroundStyle(DS.Palette.textTertiary)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(store.currentArchives) { entry in
                            HStack(spacing: 12) {
                                Text(entry.title)
                                    .font(DS.Font.meta)
                                    .lineLimit(2)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                Button("Restore") { store.restoreLocalArchive(entry.id) }
                                    .buttonStyle(.plain)
                                    .font(DS.Font.meta)
                                    .foregroundStyle(DS.Palette.accent)
                                    .accessibilityLabel("Restore \(entry.title)")
                            }
                        }
                    }
                }
                .frame(maxHeight: 280)
            }
        }
        .padding(16)
        .frame(width: 340, alignment: .leading)
    }
}
