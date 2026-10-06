import SwiftUI

enum TextFontFamily: String, CaseIterable, Identifiable {
    case system, rounded, serif
    var id: String { rawValue }
    var label: String {
        switch self {
        case .system: return "系统"
        case .rounded: return "圆角"
        case .serif: return "衬线"
        }
    }
    var design: Font.Design {
        switch self {
        case .system: return .default
        case .rounded: return .rounded
        case .serif: return .serif
        }
    }
}

enum TextFontWeight: String, CaseIterable, Identifiable {
    case regular, medium, semibold, bold
    var id: String { rawValue }
    var label: String {
        switch self {
        case .regular: return "常规"
        case .medium: return "中等"
        case .semibold: return "半粗"
        case .bold: return "粗体"
        }
    }
    var fontWeight: Font.Weight { resolve(.regular) }
    // Keep semantic emphasis when increasing the base weight.
    func resolve(_ semantic: Font.Weight) -> Font.Weight {
        switch self {
        case .regular: return semantic
        case .medium: return semantic == .regular ? .medium : semantic
        case .semibold: return semantic == .bold ? .bold : .semibold
        case .bold: return .bold
        }
    }
}

struct TypographyStyle: Equatable {
    var family: TextFontFamily = .system
    var weight: TextFontWeight = .regular
    var interfaceSize: Double = 12
    var contentSize: Double = 15
    var codeSize: Double = 13
    var lineSpacing: Double = 4

    var interfaceScale: CGFloat { bounded(interfaceSize, 10...18, fallback: 12) / 12 }
    var bodySize: CGFloat { bounded(contentSize, 12...24, fallback: 15) }
    var monoSize: CGFloat { bounded(codeSize, 10...20, fallback: 13) }
    var paragraphSpacing: CGFloat { bounded(lineSpacing, 0...10, fallback: 4) }

    private func bounded(_ value: Double, _ range: ClosedRange<Double>, fallback: Double) -> CGFloat {
        CGFloat(value.isFinite ? min(range.upperBound, max(range.lowerBound, value)) : fallback)
    }
}

/// Local preferences only. AppStorage updates existing views without rebuilding
/// the window, changing task selection or restarting any Observer connection.
struct TypographyPreferences: DynamicProperty {
    @AppStorage("monitor.text.family") var family = TextFontFamily.system
    @AppStorage("monitor.text.weight") var weight = TextFontWeight.regular
    @AppStorage("monitor.text.interfaceSize") var interfaceSize = 12.0
    @AppStorage("monitor.text.contentSize") var contentSize = 15.0
    @AppStorage("monitor.text.codeSize") var codeSize = 13.0
    @AppStorage("monitor.text.lineSpacing") var lineSpacing = 4.0

    init(store: UserDefaults? = nil) {
        _family = AppStorage(wrappedValue: .system, "monitor.text.family", store: store)
        _weight = AppStorage(wrappedValue: .regular, "monitor.text.weight", store: store)
        _interfaceSize = AppStorage(wrappedValue: 12, "monitor.text.interfaceSize", store: store)
        _contentSize = AppStorage(wrappedValue: 15, "monitor.text.contentSize", store: store)
        _codeSize = AppStorage(wrappedValue: 13, "monitor.text.codeSize", store: store)
        _lineSpacing = AppStorage(wrappedValue: 4, "monitor.text.lineSpacing", store: store)
    }

    var style: TypographyStyle {
        TypographyStyle(family: family, weight: weight, interfaceSize: interfaceSize,
                        contentSize: contentSize, codeSize: codeSize, lineSpacing: lineSpacing)
    }

    func reset() {
        family = .system; weight = .regular; interfaceSize = 12
        contentSize = 15; codeSize = 13; lineSpacing = 4
    }
}

/// Design tokens retain their original sizes and hierarchy at the default scale.
/// The font modifier observes settings independently of cached task/Activity views.
struct InterfaceFont {
    let size: CGFloat
    var weight: Font.Weight = .regular
    var design: Font.Design = .default

    func resolved(in style: TypographyStyle) -> Font {
        .system(size: size * style.interfaceScale, weight: style.weight.resolve(weight),
                design: design == .monospaced ? .monospaced : style.family.design)
    }
}

private struct InterfaceFontModifier: ViewModifier {
    let font: InterfaceFont
    private var preferences = TypographyPreferences()
    func body(content: Content) -> some View {
        content.font(font.resolved(in: preferences.style))
    }
}

extension View {
    func font(_ font: InterfaceFont) -> some View { modifier(InterfaceFontModifier(font: font)) }
    func interfaceFont(size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> some View {
        font(InterfaceFont(size: size, weight: weight, design: design))
    }
}
