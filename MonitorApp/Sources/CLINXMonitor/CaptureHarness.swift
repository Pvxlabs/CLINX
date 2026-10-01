import AppKit
import SwiftUI

/// Verification-only capture harness.
///
/// Enabled exclusively by the `CLINX_CAPTURE_DIR` environment variable, which is set by the
/// macOS acceptance run — never by the shipping app. It drives the window through the
/// synthetic scenarios and writes one PNG per screen so the rendered UI can be compared with
/// the Figma reference frames.
///
/// It renders the app's own window through `cacheDisplay(in:to:)`, so it needs no Screen
/// Recording permission (a `screencapture` from a remote session cannot get one), and it
/// adds no control surface to the app: every step is read-only.
@MainActor
enum CaptureHarness {

    struct Spec {
        let name: String
        let scenario: SyntheticScenario
        let view: MonitorView
        let selected: String?
        let width: CGFloat
        let height: CGFloat
        let dark: Bool
        let settings: Bool
    }

    /// The screens required by the acceptance matrix, at the design's reference sizes.
    static let specs: [Spec] = [
        Spec(name: "04-live-healthy", scenario: .healthy, view: .active, selected: nil, width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "05-active", scenario: .running, view: .active, selected: "task_abb594e2c1", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "06-blocked", scenario: .blocked, view: .blocked, selected: "task_4c01d8f77a", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "07-failed", scenario: .failed, view: .failed, selected: "task_e0f3a1c2d9", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "08-completed", scenario: .running, view: .completed, selected: "task_7a6b5c4d3e", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "09-stale", scenario: .stale, view: .active, selected: "task_abb594e2c1", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "10-offline", scenario: .offline, view: .active, selected: "task_4c01d8f77a", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "11-empty", scenario: .empty, view: .active, selected: nil, width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "12-synthetic-long", scenario: .long, view: .active, selected: "exec_fixture_0", width: 1440, height: 900, dark: false, settings: false),
        Spec(name: "13-settings", scenario: .running, view: .active, selected: "task_abb594e2c1", width: 1440, height: 900, dark: false, settings: true),
        Spec(name: "14-dark-blocked", scenario: .blocked, view: .blocked, selected: "task_4c01d8f77a", width: 1440, height: 900, dark: true, settings: false),
        Spec(name: "14b-dark-synthetic", scenario: .running, view: .active, selected: "task_abb594e2c1", width: 1440, height: 900, dark: true, settings: false),
        Spec(name: "15-compact-1100", scenario: .running, view: .active, selected: "task_4c01d8f77a", width: 1100, height: 720, dark: false, settings: false),
        Spec(name: "15b-compact-900", scenario: .running, view: .active, selected: "task_abb594e2c1", width: 900, height: 640, dark: false, settings: false),
    ]

    static var isEnabled: Bool {
        ProcessInfo.processInfo.environment["CLINX_CAPTURE_DIR"] != nil
    }

    static func startIfEnabled(store: MonitorStore) {
        guard let directory = ProcessInfo.processInfo.environment["CLINX_CAPTURE_DIR"] else { return }
        Task { @MainActor in
            await run(store: store, directory: URL(fileURLWithPath: directory))
        }
    }

    private static func run(store: MonitorStore, directory: URL) async {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        // Give the WindowGroup a moment to create and lay out its window.
        try? await Task.sleep(nanoseconds: 1_500_000_000)

        let geometryOnly = ProcessInfo.processInfo.environment["CLINX_CAPTURE_GEOMETRY"] == "1"
        let selectedSpecs = geometryOnly ? specs.filter {
            ["04-live-healthy", "14b-dark-synthetic", "15-compact-1100", "15b-compact-900"].contains($0.name)
        } : specs
        for spec in selectedSpecs {
            apply(spec, store: store)
            guard let window = mainWindow() else {
                NSLog("CLINX capture: no window available")
                break
            }
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            resize(window, to: spec)
            // Let the store refresh, the layout settle and the appearance redraw.
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            await checkpoint(window, name: spec.name, into: directory)
            capture(window, name: spec.name, into: directory, settings: spec.settings)
        }
        if geometryOnly {
            await captureWindowLifecycle(store: store, directory: directory)
        } else {
            await captureInteractions(store: store, directory: directory)
        }
        try? "complete".write(to: directory.appendingPathComponent("complete.txt"), atomically: true, encoding: .utf8)
        NSLog("CLINX capture: complete")
        NSApp.terminate(nil)
    }

    private static func checkpoint(_ window: NSWindow, name: String, into directory: URL) async {
        guard ProcessInfo.processInfo.environment["CLINX_CAPTURE_HOLD"] == "1" else { return }
        try? "\(name)\n\(window.windowNumber)".write(
            to: directory.appendingPathComponent("ready.txt"), atomically: true, encoding: .utf8)
        let advance = directory.appendingPathComponent("continue.txt")
        while !FileManager.default.fileExists(atPath: advance.path) {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        try? FileManager.default.removeItem(at: advance)
    }

    private static func captureWindowLifecycle(store: MonitorStore, directory: URL) async {
        guard let window = mainWindow() else { return }
        let spec = specs[0]
        apply(spec, store: store)
        resize(window, to: spec)
        try? await Task.sleep(nanoseconds: 1_400_000_000)
        let entered = await toggleFullScreen(window, notification: NSWindow.didEnterFullScreenNotification)
        if entered {
            writeGeometry(window, name: "16-fullscreen", into: directory)
        }
        var exited = false
        if entered {
            exited = await toggleFullScreen(window, notification: NSWindow.didExitFullScreenNotification)
        }
        try? "entered=\(entered)\nexited=\(exited)".write(
            to: directory.appendingPathComponent("fullscreen.txt"), atomically: true, encoding: .utf8)
        guard exited else { return }
        // Exercise deactivation/reactivation too, then capture the restored desktop.
        NSApp.deactivate()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        try? await Task.sleep(nanoseconds: 1_400_000_000)
        await checkpoint(window, name: "17-desktop-restored", into: directory)
        capture(window, name: "17-desktop-restored", into: directory, settings: false)
    }

    private static func toggleFullScreen(_ window: NSWindow, notification: Notification.Name) async -> Bool {
        var complete = false
        let token = NotificationCenter.default.addObserver(forName: notification, object: window, queue: .main) { _ in
            complete = true
        }
        defer { NotificationCenter.default.removeObserver(token) }
        window.toggleFullScreen(nil)
        for _ in 0..<100 {
            if complete { return true }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return false
    }

    /// Interaction evidence: the two interactions that change what the list contains and
    /// which row is current, captured from the same running window.
    private static func captureInteractions(store: MonitorStore, directory: URL) async {
        guard let window = mainWindow() else { return }
        NSApp.appearance = NSAppearance(named: .aqua)
        resize(window, to: Spec(name: "interaction", scenario: .running, view: .recent, selected: nil,
                                width: 1440, height: 900, dark: false, settings: false))
        store.useSynthetic(.running)
        store.view = .recent
        store.timeWindow = .week
        store.searchText = "audit"
        try? await Task.sleep(nanoseconds: 1_200_000_000)
        capture(window, name: "16-interaction-search", into: directory, settings: false)

        store.searchText = ""
        try? await Task.sleep(nanoseconds: 600_000_000)
        store.moveSelection(by: 3)
        try? await Task.sleep(nanoseconds: 1_200_000_000)
        capture(window, name: "17-interaction-keyboard-selection", into: directory, settings: false)
    }

    private static func apply(_ spec: Spec, store: MonitorStore) {
        NSApp.appearance = NSAppearance(named: spec.dark ? .darkAqua : .aqua)
        store.useSynthetic(spec.scenario)
        store.view = spec.view
        store.searchText = ""
        store.projectFilter = nil
        store.hostFilter = nil
        store.timeWindow = .week
        if let selected = spec.selected {
            store.beginSelection(selected)
        } else {
            store.clearSelection()
        }
    }

    private static func mainWindow() -> NSWindow? {
        NSApp.windows.first { $0.contentView != nil && $0.isVisible && $0.title != "Settings" }
            ?? NSApp.windows.first { $0.contentView != nil }
    }

    private static func resize(_ window: NSWindow, to spec: Spec) {
        // Set the full window frame so the capture matches the design's reference size
        // (which includes its own title bar and toolbar).
        var frame = window.frame
        frame.size = NSSize(width: spec.width, height: spec.height)
        if let screen = window.screen ?? NSScreen.main {
            frame.origin = NSPoint(x: screen.visibleFrame.minX + 24,
                                   y: screen.visibleFrame.maxY - frame.height - 24)
        }
        window.setFrame(frame, display: true)
        NSLog("CLINX capture: window frame \(Int(window.frame.width))x\(Int(window.frame.height))")
    }

    private static func capture(_ window: NSWindow, name: String, into directory: URL, settings: Bool) {
        var target = window
        if settings {
            SettingsOpener.open()
            RunLoop.current.run(until: Date().addingTimeInterval(1.5))
            let described = NSApp.windows.map { "\($0.className)/title=\($0.title)/visible=\($0.isVisible)/key=\($0.isKeyWindow)" }
            NSLog("CLINX capture windows: \(described.joined(separator: " | "))")
            target = NSApp.windows.first { $0.isVisible && $0 !== window && $0.contentView != nil } ?? window
        }
        guard let frameView = target.contentView?.superview ?? target.contentView else { return }
        frameView.layoutSubtreeIfNeeded()
        writeGeometry(target, name: name, into: directory)
        let bounds = frameView.bounds
        let scale = target.backingScaleFactor > 0 ? target.backingScaleFactor : 2
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
                                         pixelsWide: Int(bounds.width * scale),
                                         pixelsHigh: Int(bounds.height * scale),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                         isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0) else { return }
        rep.size = bounds.size
        frameView.cacheDisplay(in: bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return }
        let url = directory.appendingPathComponent("\(name).png")
        try? data.write(to: url)
        NSLog("CLINX capture: wrote \(url.path)")
        if settings, target !== window { target.orderOut(nil) }
    }

    private static func writeGeometry(_ window: NSWindow, name: String, into directory: URL) {
        func rect(_ r: NSRect) -> [String: CGFloat] {
            ["x": r.minX, "y": r.minY, "width": r.width, "height": r.height]
        }
        func topRect(_ view: NSView) -> [String: CGFloat] {
            let r = view.convert(view.bounds, to: nil)
            return rect(NSRect(x: r.minX, y: window.frame.height - r.maxY,
                               width: r.width, height: r.height))
        }
        var probes: [String: Any] = [:]
        func visit(_ view: NSView) {
            if let id = view.identifier?.rawValue, id.hasPrefix("capture.") {
                probes[String(id.dropFirst(8))] = topRect(view)
            }
            view.subviews.forEach(visit)
        }
        if let root = window.contentView?.superview { visit(root) }
        let types: [(String, NSWindow.ButtonType)] = [
            ("close", .closeButton), ("minimize", .miniaturizeButton), ("zoom", .zoomButton)
        ]
        var buttons: [String: Any] = [:]
        for (key, type) in types {
            guard let button = window.standardWindowButton(type), let parent = button.superview else { continue }
            var ancestors: [[String: Any]] = []
            var ancestor: NSView? = parent
            while let view = ancestor {
                ancestors.append(["class": view.className, "frame": rect(view.frame),
                                  "windowTopFrame": topRect(view)])
                ancestor = view.superview
            }
            buttons[key] = ["frame": rect(button.frame), "windowTopFrame": topRect(button),
                            "superview": parent.className, "superviewFrame": rect(parent.frame),
                            "superviewWindowTopFrame": topRect(parent), "hidden": button.isHidden,
                            "ancestors": ancestors]
        }
        let insets = window.contentView?.safeAreaInsets ?? NSEdgeInsets()
        let report: [String: Any] = [
            "coordinateSystem": "window top-left, points", "windowFrame": rect(window.frame),
            "windowNumber": window.windowNumber,
            "contentLayoutRect": rect(window.contentLayoutRect),
            "contentViewFrame": rect(window.contentView?.frame ?? .zero),
            "safeAreaInsets": ["top": insets.top, "bottom": insets.bottom, "left": insets.left, "right": insets.right],
            "scale": window.backingScaleFactor, "buttons": buttons, "swiftUI": probes,
            "fullSizeContentView": window.styleMask.contains(.fullSizeContentView),
            "toolbarPresent": window.toolbar != nil
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: directory.appendingPathComponent("\(name)-geometry.json"))
        } catch { NSLog("CLINX geometry capture failed: \(error)") }
    }
}
