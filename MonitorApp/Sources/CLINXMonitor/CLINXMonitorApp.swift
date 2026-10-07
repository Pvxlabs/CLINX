import AppKit
import SwiftUI

/// CLINX — a read-only macOS client for the private P620 Observer.
@main
struct CLINXMonitorApp: App {
    @NSApplicationDelegateAdaptor(MonitorAppDelegate.self) private var appDelegate
    @StateObject private var store = MonitorStore()
    @Environment(\.openWindow) private var openWindow

    var body: some Scene {
        WindowGroup("CLINX", id: "monitor") {
            MonitorRootView(store: store)
                .background(MonitorWindowRegistration(appDelegate: appDelegate))
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
    private weak var mainWindow: NSWindow?
    private var closeObserver: NSObjectProtocol?
    var openWindow: (() -> Void)?

    func installMenu(store: MonitorStore, openWindow: @escaping () -> Void) {
        self.openWindow = openWindow
        guard menuBar == nil else { return }
        menuBar = MonitorMenuBar(store: store) { [weak self] in self?.showMonitor() }
    }

    func showMonitor() {
        if let window = mainWindow {
            window.deminiaturize(nil)
            window.makeKeyAndOrderFront(nil)
        } else {
            openWindow?()
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func registerMainWindow(_ window: NSWindow) {
        guard mainWindow !== window else { return }
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
        mainWindow = window
        // AppKit can retain a closed SwiftUI window. Never restore that stale
        // object; let the scene create its next live content surface instead.
        closeObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: window, queue: .main
        ) { [weak self, weak window] _ in
            guard let self, self.mainWindow === window else { return }
            self.mainWindow = nil
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // A visible Settings window must not prevent a Dock click restoring the main scene.
        showMonitor()
        return false
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

    deinit {
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
    }
}

/// Register the actual scene window without depending on its display title or
/// AppKit's visibility-dependent canBecomeMain property.
private struct MonitorWindowRegistration: NSViewRepresentable {
    let appDelegate: MonitorAppDelegate

    func makeNSView(context: Context) -> WindowBackingView {
        let view = WindowBackingView()
        view.register = { [weak appDelegate] in appDelegate?.registerMainWindow($0) }
        return view
    }

    func updateNSView(_ nsView: WindowBackingView, context: Context) {
        if let window = nsView.window { appDelegate.registerMainWindow(window) }
    }

    final class WindowBackingView: NSView {
        var register: ((NSWindow) -> Void)?

        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let window { register?(window) }
        }
    }
}
