import AppKit

/// AppKit owns menu tracking; read the shared observer only when the menu opens.
/// Polling never rebuilds an open menu or interferes with outside-click dismissal.
@MainActor
final class MonitorMenuBar: NSObject, NSMenuDelegate {
    private let store: MonitorStore
    private let openWindow: () -> Void
    private let statusItem: NSStatusItem
    private let menu = NSMenu()
    private let connectionItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var categoryItems: [(MonitorView, NSMenuItem)] = []
    private var refreshItem: NSMenuItem!
    private var settingsItem: NSMenuItem!

    init(store: MonitorStore, openWindow: @escaping () -> Void) {
        self.store = store
        self.openWindow = openWindow
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()
        statusItem.button?.image = Self.icon
        statusItem.button?.toolTip = "CLINX"
        statusItem.button?.setAccessibilityLabel("CLINX")
        menu.autoenablesItems = false
        menu.delegate = self
        let title = NSMenuItem(title: "CLINX", action: nil, keyEquivalent: "")
        title.isEnabled = false
        connectionItem.isEnabled = false
        menu.addItem(title)
        menu.addItem(connectionItem)
        menu.addItem(.separator())
        add("Open CLINX", action: #selector(openMonitor))
        for view in [MonitorView.active, .blocked, .failed, .recent, .completed] {
            let item = add(view.label, action: #selector(openCategory(_:)))
            item.representedObject = view.rawValue
            categoryItems.append((view, item))
        }
        menu.addItem(.separator())
        refreshItem = add("Refresh Now", action: #selector(refresh))
        settingsItem = add("Settings…", action: #selector(openSettings))
        menu.addItem(.separator())
        add("Quit CLINX", action: #selector(quit))
        statusItem.menu = menu
    }

    @discardableResult
    private func add(_ title: String, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
        return item
    }

    func menuWillOpen(_ menu: NSMenu) {
        connectionItem.title = "\(store.runtimeEnvironment.prefix(18)) · \(store.runtimeStatus.connection)"
        let counts = store.counts
        for (view, item) in categoryItems {
            item.title = "\(view.label) · \(counts[view, default: 0])"
        }
        refreshItem.isEnabled = !store.isRefreshing
        settingsItem.isEnabled = appSettingsItem?.action != nil
    }

    // Finish native tracking before activating a window or dispatching an app command.
    private func afterClosing(_ action: @escaping () -> Void) {
        menu.cancelTracking()
        DispatchQueue.main.async(execute: action)
    }

    @objc private func openMonitor() { afterClosing { self.showMonitor() } }

    @objc private func openCategory(_ item: NSMenuItem) {
        guard let rawValue = item.representedObject as? String,
              let view = MonitorView(rawValue: rawValue) else { return }
        afterClosing {
            self.store.view = view
            self.showMonitor()
        }
    }

    private func showMonitor() {
        openWindow()
    }

    @objc private func refresh() {
        afterClosing { Task { await self.store.refresh() } }
    }

    private var appSettingsItem: NSMenuItem? {
        NSApp.mainMenu?.items.first?.submenu?.items.first { $0.keyEquivalent == "," }
    }

    @objc private func openSettings() {
        afterClosing {
            NSApp.activate(ignoringOtherApps: true)
            if let item = self.appSettingsItem, let action = item.action {
                NSApp.sendAction(action, to: item.target, from: item)
            }
        }
    }

    @objc private func quit() { afterClosing { NSApp.terminate(nil) } }

    /// Template artwork lets macOS supply the correct light/dark/selected appearance.
    static let icon: NSImage = {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            let scale: CGFloat = 16 / 486
            let center = NSPoint(x: 9 + 14 * scale, y: 9)
            NSColor.black.set()
            let ring = NSBezierPath()
            ring.appendArc(withCenter: center, radius: 210 * scale,
                           startAngle: 29.86, endAngle: 330.14)
            ring.lineWidth = 66 * scale
            ring.lineCapStyle = .round
            ring.stroke()
            NSBezierPath(ovalIn: NSRect(x: center.x + (32 - 64) * scale,
                                       y: center.y - 64 * scale,
                                       width: 128 * scale, height: 128 * scale)).fill()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "CLINX"
        return image
    }()
}
