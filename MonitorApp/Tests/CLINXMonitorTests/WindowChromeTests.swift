import AppKit
import SwiftUI
import XCTest
@testable import CLINXMonitor

final class WindowChromeTests: XCTestCase {
    @MainActor
    func testFullSizeWindowKeepsNativeButtonsInsideTheSingleHeader() {
        let window = makeWindow()
        defer { window.close() }
        FullSizeContentConfigurator.configure(window)

        XCTAssertEqual(DS.Metric.contentHeaderHeight, 48)
        XCTAssertTrue(window.styleMask.contains(.fullSizeContentView))
        XCTAssertTrue(window.titlebarAppearsTransparent)
        XCTAssertEqual(window.titleVisibility, .hidden)
        XCTAssertNil(window.toolbar, "the header must not acquire a second system toolbar")
        assertAlignment(window)
    }

    @MainActor
    func testBridgeReconcilesAppKitRelayoutWithoutATimer() {
        let window = makeWindow()
        defer { window.close() }
        let host = NSHostingView(rootView: Color.clear.background(FullSizeContentConfigurator()))
        window.contentView = host
        host.layoutSubtreeIfNeeded()

        for width in [1440.0, 1100, 900, 1440] {
            window.setFrame(NSRect(x: 0, y: 0, width: width, height: 900), display: false)
            // Model AppKit restoring its standard titlebar layout during an update.
            let button = window.standardWindowButton(.closeButton)!
            button.setFrameOrigin(NSPoint(x: button.frame.minX, y: 6))
            NotificationCenter.default.post(name: NSWindow.didUpdateNotification, object: window)
            assertAlignment(window)
        }
    }

    func testInsetPanelPreservesTheRoundedCornerCenter() {
        let shape = PanelShape(radius: 10, corners: [.topLeft, .bottomLeft]).inset(by: 2)
        let path = shape.path(in: CGRect(x: 0, y: 0, width: 384, height: 812))
        XCTAssertEqual(path.boundingRect, CGRect(x: 2, y: 2, width: 380, height: 808))
        // On the inset quarter-circle around (10, 802), rather than (12, 800).
        XCTAssertTrue(path.contains(CGPoint(x: 7, y: 809)))
        XCTAssertFalse(path.contains(CGPoint(x: 4, y: 808)))
    }

    @MainActor
    private func makeWindow() -> NSWindow {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        return window
    }

    @MainActor
    private func assertAlignment(_ window: NSWindow, file: StaticString = #filePath, line: UInt = #line) {
        for type in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            guard let button = window.standardWindowButton(type), let parent = button.superview else {
                XCTFail("missing native window button", file: file, line: line)
                continue
            }
            let rect = button.convert(button.bounds, to: nil)
            XCTAssertEqual(window.frame.height - rect.midY, 24, accuracy: 0.01, file: file, line: line)
            XCTAssertTrue(parent.bounds.contains(button.frame), "native hit region must not clip",
                          file: file, line: line)
            var ancestor: NSView? = parent.superview
            while let view = ancestor {
                XCTAssertTrue(view.bounds.contains(button.convert(button.bounds, to: view)),
                              "an enclosing titlebar container must not clip the native button",
                              file: file, line: line)
                ancestor = view.superview
            }
        }
    }
}
