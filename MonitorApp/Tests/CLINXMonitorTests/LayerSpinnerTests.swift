import AppKit
import QuartzCore
import XCTest
@testable import CLINXMonitor

final class LayerSpinnerTests: XCTestCase {
    @MainActor
    func testRefreshAndAppearanceChangesKeepAnimationAndStrokeInsideBacking() throws {
        _ = NSApplication.shared
        let view = LayerSpinner.SpinnerView(frame: NSRect(x: 0, y: 0, width: 16, height: 16))
        view.layout()
        let layers = try XCTUnwrap(view.layer?.sublayers)
        let arc = try XCTUnwrap(layers.last as? CAShapeLayer)
        let animation = try XCTUnwrap(arc.animation(forKey: "spin") as? CABasicAnimation)

        for appearance in [NSAppearance.Name.aqua, .darkAqua] {
            view.appearance = NSAppearance(named: appearance)
            for _ in 0..<10 {
                view.color = .controlAccentColor
                view.layout()
            }
            XCTAssertTrue(view.layer?.sublayers?.last === arc)
            let current = try XCTUnwrap(arc.animation(forKey: "spin") as? CABasicAnimation)
            XCTAssertEqual(current.beginTime, animation.beginTime)
            XCTAssertEqual(current.duration, 1.4)
            XCTAssertEqual(current.repeatCount, .infinity)
            XCTAssertEqual(arc.strokeEnd, 0.25)
            let pathBounds = try XCTUnwrap(arc.path).boundingBoxOfPath
            XCTAssertEqual(pathBounds.width, 12)
            XCTAssertTrue(view.bounds.contains(pathBounds.insetBy(dx: -arc.lineWidth / 2,
                                                                 dy: -arc.lineWidth / 2)))
            XCTAssertNil(arc.animation(forKey: "strokeColor"))
        }
    }
}
