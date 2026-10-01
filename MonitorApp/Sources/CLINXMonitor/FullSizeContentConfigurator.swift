import AppKit
import SwiftUI

/// One full-size content surface. AppKit lays out its titlebar again on resize and
/// activation, so reconcile native button geometry after each window update.
struct FullSizeContentConfigurator: NSViewRepresentable {
    var headerTopInset: CGFloat = 0

    func makeNSView(context: Context) -> NSView {
        let view = WindowBackingView()
        view.headerTopInset = headerTopInset
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        guard let view = nsView as? WindowBackingView else { return }
        view.headerTopInset = headerTopInset
        view.configure()
    }

    static func configure(_ window: NSWindow, headerTopInset: CGFloat = 0) {
        if !window.styleMask.contains(.fullSizeContentView) {
            window.styleMask.insert(.fullSizeContentView)
        }
        if !window.titlebarAppearsTransparent { window.titlebarAppearsTransparent = true }
        if window.titleVisibility != .hidden { window.titleVisibility = .hidden }
        window.isMovableByWindowBackground = true

        // macOS owns fullscreen chrome (and its reveal animation). Restore our header
        // geometry once the window returns to its regular content surface.
        guard !window.styleMask.contains(.fullScreen) else { return }
        let buttons = [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton]
            .compactMap { window.standardWindowButton($0) }
        guard let container = buttons.first?.superview, let parent = container.superview,
              buttons.allSatisfy({ $0.superview === container }) else { return }

        // Enlarge the actual titlebar container, keeping its window-space top fixed.
        // Moving buttons below a 28pt container alone clips their native hit regions.
        let height = headerTopInset + DS.Metric.contentHeaderHeight
        // The button view and its enclosing titlebar container both clip at the
        // native 28pt height. Resize outer-to-inner, without touching the theme frame.
        for view in [parent, container] {
            guard let superview = view.superview else { continue }
            if abs(view.frame.height - height) > 0.01 {
                var frame = view.frame
                if !superview.isFlipped { frame.origin.y += frame.height - height }
                frame.size.height = height
                view.frame = frame
            }
        }
        let windowCenter = NSPoint(x: 0, y: window.frame.height - headerTopInset - DS.Metric.contentHeaderHeight / 2)
        let center = container.convert(windowCenter, from: nil).y
        for button in buttons where abs(button.frame.midY - center) > 0.01 {
            var frame = button.frame
            frame.origin.y = center - frame.height / 2
            button.frame = frame
        }
    }
}

private final class WindowBackingView: NSView {
    var headerTopInset: CGFloat = 0
    private var observers: [NSObjectProtocol] = []
    private var configuring = false

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        removeObservers()
        if let window {
            for name in [NSWindow.didUpdateNotification, NSWindow.didResizeNotification,
                         NSWindow.didEndLiveResizeNotification, NSWindow.didBecomeKeyNotification,
                         NSWindow.didEnterFullScreenNotification, NSWindow.didExitFullScreenNotification,
                         NSWindow.didChangeBackingPropertiesNotification] {
                observers.append(NotificationCenter.default.addObserver(
                    forName: name, object: window, queue: .main
                ) { [weak self] _ in self?.configure() })
            }
        }
        configure()
    }

    override func layout() {
        super.layout()
        configure()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        configure()
    }

    func configure() {
        guard !configuring, let window else { return }
        configuring = true
        defer { configuring = false }
        FullSizeContentConfigurator.configure(window, headerTopInset: headerTopInset)
    }

    private func removeObservers() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers.removeAll()
    }

    deinit { removeObservers() }
}
