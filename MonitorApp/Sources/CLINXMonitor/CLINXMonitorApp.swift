import AppKit
import SwiftUI

/// CLINX Monitor — a read-only macOS client for the private P620 Observer.
@main
struct CLINXMonitorApp: App {
    @NSApplicationDelegateAdaptor(MonitorAppDelegate.self) private var appDelegate
    @StateObject private var store = MonitorStore()
    @Environment(\.openWindow) private var openWindow

    var body: some Scene {
        WindowGroup("CLINX Monitor", id: "monitor") {
            MonitorRootView(store: store)
                .font(DS.Font.body)
                .frame(minWidth: DS.Metric.windowMinWidth, minHeight: DS.Metric.windowMinHeight)
                .onAppear {
                    appDelegate.installMenu(store: store) { openWindow(id: "monitor") }
                    store.start()
                    CaptureHarness.startIfEnabled(store: store)
                }
                // The shared observer remains live when only the menu bar is visible.
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1440, height: 900)
        .windowResizability(.contentMinSize)
        .commands { commands }

        Settings {
            MonitorSettingsView(store: store)
                .font(DS.Font.body)
        }

    }

    @CommandsBuilder private var commands: some Commands {
        CommandGroup(after: .toolbar) {
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

@MainActor
final class MonitorAppDelegate: NSObject, NSApplicationDelegate {
    private var menuBar: MonitorMenuBar?

    func installMenu(store: MonitorStore, openWindow: @escaping () -> Void) {
        guard menuBar == nil else { return }
        menuBar = MonitorMenuBar(store: store, openWindow: openWindow)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        // In-place local rebuilds can leave Launch Services serving the previous icon.
        // Use this bundle's artwork for the running Dock tile, without the named-image cache.
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let icon = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = icon
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}
