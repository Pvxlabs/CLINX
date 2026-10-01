import SwiftUI

/// The CLINX Monitor mark — an open ring observing one execution, “watched, never touched”.
///
/// Figma source of truth: `src/monitor/AppIcon.tsx` (page 17 “App Icon”) and its toolbar use
/// in `src/monitor/MonitorWindow.tsx` (`<AppIcon size={24} flat />`).
///
/// Geometry is taken from the design's 1024 grid: 824 tile at (100,100) with a 185 corner
/// radius, an open ring of radius 210 and 66 stroke centred at (474,500) with its 59.7°
/// opening on the right, and the indigo execution dot (r 64) at (506,500).
/// `Scripts/make-app-icon.swift` draws the same figure for the bundle icon.
struct AppMark: View {
    var size: CGFloat = 24
    /// The design drops the drop shadow below 64px (`flat`).
    var flat = true

    private var scale: CGFloat { size / 1024 }

    var body: some View {
        ZStack {
            tile
            edgeHighlight
            ring
            executionDot
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private var tile: some View {
        RoundedRectangle(cornerRadius: 185 * scale, style: .continuous)
            .fill(LinearGradient(colors: [Color(hex: 0x2B2C31), Color(hex: 0x0D0E10)],
                                 startPoint: .top, endPoint: .bottom))
            .frame(width: 824 * scale, height: 824 * scale)
            .shadow(color: flat ? .clear : .black.opacity(0.32), radius: 16 * scale, x: 0, y: 14 * scale)
    }

    private var edgeHighlight: some View {
        RoundedRectangle(cornerRadius: 183.5 * scale, style: .continuous)
            .strokeBorder(LinearGradient(colors: [.white.opacity(0.16), .white.opacity(0)],
                                         startPoint: .top, endPoint: .bottom),
                          lineWidth: 3 * scale)
            .frame(width: 821 * scale, height: 821 * scale)
    }

    /// `Circle().trim` starts at 3 o'clock and runs clockwise, so the design's opening —
    /// centred on 0° and 59.7° wide — falls out of the trim range directly.
    private var ring: some View {
        Circle()
            .trim(from: 29.86 / 360, to: 330.14 / 360)
            .stroke(Color(hex: 0xF4F4F5),
                    style: StrokeStyle(lineWidth: 66 * scale, lineCap: .round, lineJoin: .round))
            .frame(width: 420 * scale, height: 420 * scale)
            .offset(x: (474 - 512) * scale, y: (500 - 512) * scale)
    }

    private var executionDot: some View {
        Circle()
            .fill(Color(hex: 0x7C84E8))
            .frame(width: 128 * scale, height: 128 * scale)
            .offset(x: (506 - 512) * scale, y: (500 - 512) * scale)
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}
