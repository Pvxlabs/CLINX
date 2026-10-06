import SwiftUI

struct NavigationHistoryView: View {
    @ObservedObject var store: MonitorStore
    let dismiss: () -> Void

    private var recentVisits: [(offset: Int, element: NavigationEntry)] {
        var seen = Set<String>()
        return store.navigationHistory.enumerated().reversed().filter { seen.insert($0.element.taskRef).inserted }
    }

    var body: some View {
        let visits = recentVisits
        VStack(alignment: .leading, spacing: 4) {
            Text("Recently viewed")
                .interfaceFont(size: 13)
                .foregroundStyle(DS.Palette.textSecondary)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            if visits.isEmpty {
                Text("Tasks you open in this window will appear here.")
                    .font(DS.Font.body)
                    .foregroundStyle(DS.Palette.textTertiary)
                    .padding(12)
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(visits, id: \.element.id) { index, entry in
                            let task = store.allTasks.first { $0.taskRef == entry.taskRef }
                            HistoryRow(entry: entry, title: task?.titleText ?? entry.title,
                                       status: task.map { store.status(of: $0) } ?? entry.status) {
                                store.openHistory(at: index)
                                dismiss()
                            }
                        }
                    }
                }
                .frame(height: min(CGFloat(visits.count) * 34, 442))
            }
        }
        .padding(6)
        .background {
            Button("Close history", action: dismiss)
                .keyboardShortcut(.cancelAction)
                .hidden()
                .accessibilityHidden(true)
        }
    }
}

private struct HistoryRow: View {
    let entry: NavigationEntry
    let title: String
    let status: MonitorStatus
    let open: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: open) {
            HStack(spacing: 10) {
                Text(entry.taskRef)
                    .font(DS.Font.mono(11))
                    .foregroundStyle(DS.Palette.textSecondary)
                    .lineLimit(1).truncationMode(.middle)
                    .frame(width: 92, alignment: .leading)
                StatusGlyphView(glyph: status.glyph, color: status.color, size: 12)
                    .frame(width: 14)
                Text(title)
                    .interfaceFont(size: 13, weight: .medium)
                    .foregroundStyle(DS.Palette.textPrimary)
                    .lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(RoundedRectangle(cornerRadius: 8).fill(hovering ? DS.Palette.hover : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("\(entry.taskRef) · \(title)")
        .accessibilityLabel("\(entry.taskRef), \(status.label), \(title)")
    }
}
