import AppKit
import QuartzCore
import SwiftUI

/// Animate only the arc's compositor layer, leaving the surrounding SwiftUI panel idle.
struct LayerSpinner: NSViewRepresentable {
    let color: Color

    func makeNSView(context: Context) -> SpinnerView { SpinnerView() }
    func updateNSView(_ view: SpinnerView, context: Context) { view.color = NSColor(color) }

    final class SpinnerView: NSView {
        private let track = CAShapeLayer()
        private let arc = CAShapeLayer()
        var color: NSColor = .controlAccentColor { didSet { updateColors() } }

        override init(frame: NSRect) {
            super.init(frame: frame)
            wantsLayer = true
            for shape in [track, arc] {
                shape.fillColor = nil
                shape.lineWidth = 1.6
                layer?.addSublayer(shape)
            }
            arc.strokeEnd = 0.25
            arc.lineCap = .round
            let rotation = CABasicAnimation(keyPath: "transform.rotation.z")
            rotation.fromValue = 0
            rotation.toValue = 2 * Double.pi
            rotation.duration = 1.4
            rotation.repeatCount = .infinity
            arc.add(rotation, forKey: "spin")
            updateColors()
        }

        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
        override var isFlipped: Bool { true }
        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        override func layout() {
            super.layout()
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            for shape in [track, arc] {
                shape.frame = bounds
                shape.path = CGPath(ellipseIn: bounds.insetBy(dx: 2, dy: 2), transform: nil)
                shape.contentsScale = window?.backingScaleFactor ?? 2
            }
            CATransaction.commit()
        }

        override func viewDidChangeEffectiveAppearance() {
            super.viewDidChangeEffectiveAppearance()
            updateColors()
        }

        private func updateColors() {
            effectiveAppearance.performAsCurrentDrawingAppearance {
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                track.strokeColor = color.withAlphaComponent(color.alphaComponent * 0.25).cgColor
                arc.strokeColor = color.cgColor
                CATransaction.commit()
            }
        }
    }
}
