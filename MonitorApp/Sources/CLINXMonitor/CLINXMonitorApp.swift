import SwiftUI

/// CLINX Monitor — a read-only macOS client for the private P620 Observer.
///
/// The redesign is a desktop window (Sidebar · List · Inspector) rather than a menu bar
/// extra: the Figma source lists “Menu bar extra with attention count” under
/// *Future suggestions — NOT in Phase 2 UI*.
@main
struct CLINXMonitorApp: App {
    @StateObject private var store = MonitorStore()

    var body: some Scene {
        WindowGroup {
            MonitorRootView(store: store)
                .frame(minWidth: DS.Metric.windowMinWidth, minHeight: DS.Metric.windowMinHeight)
                .onAppear {
                    store.start()
                    CaptureHarness.startIfEnabled(store: store)
                }
                .onDisappear { store.stop() }
        }
        .defaultSize(width: 1440, height: 900)
        .windowResizability(.contentMinSize)
        .commands { commands }

        Settings {
            MonitorSettingsView(store: store)
        }
    }

    @CommandsBuilder private var commands: some Commands {
        CommandGroup(after: .toolbar) {
            Button("Search Executions") { store.requestSearchFocus() }
                .keyboardShortcut("k", modifiers: .command)
            Button("Refresh Now") { Task { await store.refresh() } }
                .keyboardShortcut("r", modifiers: .command)
            Divider()
            ForEach(MonitorView.allCases) { view in
                Button("Go to \(view.label)") { store.view = view }
                    .keyboardShortcut(KeyEquivalent(Character("\(view.shortcutIndex)")), modifiers: .command)
            }
        }
    }
}
