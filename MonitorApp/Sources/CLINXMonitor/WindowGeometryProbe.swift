import AppKit
import SwiftUI

/// Acceptance-only probes. The background views neither draw nor receive input.
struct WindowGeometryProbe: NSViewRepresentable {
    let name: String

    func makeNSView(context: Context) -> NSView {
        let view = ProbeView()
        view.identifier = NSUserInterfaceItemIdentifier("capture.\(name)")
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    private final class ProbeView: NSView {
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
    }
}

extension View {
    @ViewBuilder
    func captureGeometry(_ name: String) -> some View {
        if CaptureHarness.isEnabled {
            background(WindowGeometryProbe(name: name))
        } else {
            self
        }
    }
}
