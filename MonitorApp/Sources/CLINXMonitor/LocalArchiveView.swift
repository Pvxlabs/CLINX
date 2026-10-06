import SwiftUI

struct LocalArchiveView: View {
    @ObservedObject var store: MonitorStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "archivebox").foregroundStyle(DS.Palette.textSecondary)
                Text("Archived on this Mac").font(DS.Font.bodyEmphasis)
                Spacer()
                Text("\(store.currentArchives.count)")
                    .font(DS.Font.meta).monospacedDigit()
                    .foregroundStyle(DS.Palette.textTertiary)
            }
            .padding(16)
            Divider().overlay(DS.Palette.divider)
            if store.currentArchives.isEmpty {
                Text("No archived executions")
                    .font(DS.Font.meta)
                    .foregroundStyle(DS.Palette.textTertiary)
                    .frame(maxWidth: .infinity, minHeight: 88)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(store.currentArchives.reversed()) { entry in
                            ArchiveRestoreRow(entry: entry) { store.restoreLocalArchive(entry.id) }
                        }
                    }
                    .padding(8)
                }
                .frame(height: min(CGFloat(store.currentArchives.count) * 46 + 16, 292))
            }
            Divider().overlay(DS.Palette.divider)
            Text("Hidden on this Mac. Restore to show in the list.")
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textTertiary)
                .padding(16)
        }
        .foregroundStyle(DS.Palette.textPrimary)
        .frame(width: 360, alignment: .leading)
        .background(DS.Palette.surface)
    }
}

private struct ArchiveRestoreRow: View {
    let entry: LocalArchiveEntry
    let restore: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 12) {
            Text(entry.title)
                .font(DS.Font.body)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .help(entry.title)
            Button(action: restore) {
                Image(systemName: "arrow.uturn.backward")
                    .interfaceFont(size: 12)
                    .frame(width: 24, height: 24)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(hovering ? DS.Palette.accent : DS.Palette.textSecondary)
            .help("Restore to the list")
            .accessibilityLabel("Restore \(entry.title)")
        }
        .padding(.horizontal, 10)
        .frame(height: 44)
        .background(RoundedRectangle(cornerRadius: 7).fill(hovering ? DS.Palette.hover : .clear))
        .onHover { hovering = $0 }
    }
}
