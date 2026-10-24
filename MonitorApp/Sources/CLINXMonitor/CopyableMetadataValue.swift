import AppKit
import SwiftUI

@MainActor
final class ClipboardFeedback: ObservableObject {
    enum Status: Equatable { case idle, copied, failed }
    @Published private(set) var status: Status = .idle
    private let write: (String) -> Bool
    private var resetTask: Task<Void, Never>?

    init(write: @escaping (String) -> Bool = { value in
        NSPasteboard.general.clearContents()
        return NSPasteboard.general.setString(value, forType: .string)
    }) {
        self.write = write
    }

    func copy(_ value: String) {
        clear()
        status = write(value) ? .copied : .failed
        resetTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 1_500_000_000) }
            catch { return }
            self?.status = .idle
        }
    }

    func clear() {
        resetTask?.cancel()
        resetTask = nil
        status = .idle
    }
}

/// The value itself is the copy target; no adjacent copy icon competes with it.
@MainActor
struct CopyableMetadataValue: View {
    let label: String
    let value: String
    var displayText: String? = nil
    @StateObject private var feedback: ClipboardFeedback
    @State private var hovering = false

    init(label: String, value: String, displayText: String? = nil, feedback: ClipboardFeedback? = nil) {
        self.label = label
        self.value = value
        self.displayText = displayText
        _feedback = StateObject(wrappedValue: feedback ?? ClipboardFeedback())
    }

    var body: some View {
        HStack(spacing: 8) {
            Button { feedback.copy(value) } label: {
                Text(displayText ?? value)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(hovering ? DS.Palette.accent : DS.Palette.textPrimary)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Click to copy \(label.lowercased())\n\(value)")
            .accessibilityLabel("Copy \(label)")
            .accessibilityValue(value)
            .onHover { hovering = $0 }
            if feedback.status == .copied {
                Label("Copied", systemImage: "checkmark")
                    .foregroundStyle(DS.Palette.success)
                    .fixedSize()
                    .accessibilityLabel("Copied to clipboard")
            } else if feedback.status == .failed {
                Text("Copy failed")
                    .foregroundStyle(DS.Palette.error)
                    .fixedSize()
            }
        }
        .accessibilityElement(children: .contain)
        .onChange(of: value) { _ in feedback.clear() }
        .onDisappear { feedback.clear() }
    }
}
