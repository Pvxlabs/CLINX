import AppKit
import SwiftUI

// Shared presentation components. Geometry, colour and typography come from the Figma
// "Components" and "Design Tokens" pages; every value resolves through `DS`.

// MARK: - Status glyph

/// Each status has a unique shape, so state never depends on colour alone.
struct StatusGlyphView: View {
    let glyph: StatusGlyph
    let color: Color
    var size: CGFloat = 12

    @State private var spinning = false

    var body: some View {
        Group {
            switch glyph {
            case .spinner:
                ZStack {
                    Circle().stroke(color.opacity(0.25), lineWidth: 1.6)
                    Circle()
                        .trim(from: 0, to: 0.25)
                        .stroke(color, style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
                        .rotationEffect(.degrees(spinning ? 360 : 0))
                        .animation(.linear(duration: 1.4).repeatForever(autoreverses: false), value: spinning)
                }
                .onAppear { spinning = true }
            case .octagon:
                ZStack {
                    Image(systemName: "octagon.fill").resizable().foregroundStyle(color)
                    Capsule().fill(.white).frame(width: size * 0.34, height: 1.5)
                }
            case .squareCross:
                Image(systemName: "xmark.square.fill").resizable().foregroundStyle(color)
            case .circleCheck:
                Image(systemName: "checkmark.circle.fill").resizable().foregroundStyle(color)
            case .slash:
                Image(systemName: "slash.circle").resizable().foregroundStyle(color)
            case .triangle:
                Image(systemName: "exclamationmark.triangle.fill").resizable().foregroundStyle(color)
            case .dashedRing:
                Image(systemName: "circle.dashed").resizable().foregroundStyle(color)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

struct StatusIcon: View {
    let status: MonitorStatus
    var size: CGFloat = 12

    var body: some View {
        StatusGlyphView(glyph: status.glyph, color: status.color, size: size)
            .help(status.label)
            .accessibilityLabel(status.label)
    }
}

// MARK: - Status pill

struct StatusPill: View {
    let status: MonitorStatus
    var compact = false

    var body: some View {
        HStack(spacing: 6) {
            StatusIcon(status: status, size: compact ? 10 : 12)
            Text(status.label)
                .font(.system(size: compact ? 11 : 12, weight: .medium))
                .foregroundStyle(DS.Palette.textPrimary)
        }
        .padding(.horizontal, compact ? 6 : 8)
        .frame(height: compact ? 18 : 22)
        .background(Capsule().fill(DS.Palette.surface))
        .overlay(Capsule().strokeBorder(DS.Palette.border, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Stage badge

/// The row's stage chip. Figma fixes its metrics at 16pt tall with 6pt horizontal
/// padding, the label set at 8pt mono and the chip hugging the text — no fixed width,
/// no leftover slack, and no fill: the panel already supplies the surface.
struct StageBadge: View {
    let stage: String
    var tone: Tone = .neutral

    enum Tone: Equatable { case neutral, warn, error }

    enum Metrics {
        static let height: CGFloat = 16
        static let horizontalPadding: CGFloat = 6
        static let textSize: CGFloat = 8
    }

    private var color: Color {
        switch tone {
        case .neutral: return DS.Palette.textSecondary
        case .warn: return DS.Palette.warning
        case .error: return DS.Palette.error
        }
    }

    /// Neutral chips take the shared hairline; warn/error keep their tone as a tint.
    private var border: Color {
        switch tone {
        case .neutral: return DS.Palette.border
        case .warn, .error: return color.opacity(0.35)
        }
    }

    var body: some View {
        Text(stage)
            .font(DS.Font.mono(Metrics.textSize))
            .foregroundStyle(color)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.horizontal, Metrics.horizontalPadding)
            .frame(height: Metrics.height)
            .overlay(RoundedRectangle(cornerRadius: DS.Metric.controlRadius)
                .strokeBorder(border, lineWidth: 1))
    }
}

extension StageBadge.Tone {
    /// Row status → chip tone. One shared mapping for every list, so no screen invents its
    /// own colour rule for a stage chip.
    static func forStatus(_ status: MonitorStatus) -> StageBadge.Tone {
        switch status {
        case .blocked: return .warn
        case .failed: return .error
        default: return .neutral
        }
    }
}

// MARK: - Runtime status

/// Environment and connection merged into the one `RuntimeStatus` semantic
/// (Figma component `RuntimeStatus`), docked at the window's bottom-right.
///
/// Finalized design: **transparent**. There is no card, no fill, no stroke and no
/// capsule — only the status dot and the two labels. The element is informational and
/// read-only, so it must never grow a container layer or an action affordance.
struct RuntimeStatus: View {
    let state: RuntimeStatusState

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(state.color)
                .frame(width: 7, height: 7)
            Text(state.environment)
                .font(DS.Font.mono(10, weight: .bold))
                .foregroundStyle(DS.Palette.textPrimary)
            Text("·")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DS.Palette.textTertiary)
            Text(state.connection)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(DS.Palette.textPrimary)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(state.environment), \(state.connection)")
        .help("Runtime status — \(state.environment) \(state.connection.lowercased()). Read-only.")
    }
}

// MARK: - Freshness

struct FreshnessTag: View {
    let freshness: Freshness

    var body: some View {
        Text(freshness.label)
            .font(DS.Font.monoMicro)
            .tracking(0.6)
            .foregroundStyle(freshness.color)
            .padding(.horizontal, 4)
            .frame(height: 14)
            .background(RoundedRectangle(cornerRadius: 3).fill(.clear))
            .overlay(RoundedRectangle(cornerRadius: 3).strokeBorder(freshness.color.opacity(0.4), lineWidth: 1))
            .help("Data authority for this list")
    }
}

// MARK: - Progress

struct ProgressIndicator: View {
    let progress: ProgressPresentation

    var body: some View {
        switch progress {
        case .unavailable:
            Text("Progress unavailable")
                .font(DS.Font.meta)
                .foregroundStyle(DS.Palette.textTertiary)
                .underline()
                .help(ProgressPresentation.unavailableReason)
        case .indeterminate(let label, let done):
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(label).font(DS.Font.meta).foregroundStyle(DS.Palette.textPrimary)
                    Text("· In progress").font(DS.Font.meta).foregroundStyle(DS.Palette.textSecondary)
                }
                if let done {
                    Text("\(done) completed steps")
                        .font(DS.Font.meta).monospacedDigit()
                        .foregroundStyle(DS.Palette.textTertiary)
                }
            }
            .help(ProgressPresentation.unavailableReason)
        case .determinate(let label, let done, let total):
            let fraction = total > 0 ? min(1, Double(done) / Double(total)) : 0
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text(label).font(DS.Font.meta).foregroundStyle(DS.Palette.textPrimary)
                    Spacer(minLength: 8)
                    Text("\(done) / \(total)")
                        .font(DS.Font.meta).monospacedDigit()
                        .foregroundStyle(DS.Palette.textSecondary)
                    Text("· \(Int((fraction * 100).rounded()))%")
                        .font(DS.Font.meta).monospacedDigit()
                        .foregroundStyle(DS.Palette.textTertiary)
                }
                GeometryReader { geometry in
                    ZStack(alignment: .leading) {
                        Capsule().fill(DS.Palette.surfaceSecondary)
                            .overlay(Capsule().strokeBorder(DS.Palette.divider, lineWidth: 1))
                        Capsule()
                            .fill(fraction >= 1 ? DS.Palette.success : DS.Palette.running)
                            .frame(width: max(0, geometry.size.width * fraction))
                    }
                }
                .frame(height: 4)
            }
        }
    }
}

// MARK: - Small controls

struct Kbd: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 10))
            .foregroundStyle(DS.Palette.textTertiary)
            .padding(.horizontal, 4)
            .frame(minWidth: 16, minHeight: 16)
            .background(RoundedRectangle(cornerRadius: 4).fill(DS.Palette.surfaceSecondary))
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(DS.Palette.border, lineWidth: 1))
    }
}

struct CopyButton: View {
    let text: String
    var label: String = "Copy"

    @State private var copied = false

    var body: some View {
        Button {
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            pasteboard.setString(text, forType: .string)
            copied = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { copied = false }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 10))
                Text(copied ? "Copied" : label)
                    .font(DS.Font.meta)
            }
            .foregroundStyle(DS.Palette.textSecondary)
            .padding(.horizontal, 6)
            .frame(height: 20)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Copy \(label.lowercased())")
    }
}

/// Collapsed-by-default evidence, per the handoff: nothing raw is expanded by default.
struct EvidenceDisclosure: View {
    let title: String
    let body_: String

    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                open.toggle()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .rotationEffect(.degrees(open ? 90 : 0))
                    Text(open ? "Hide \(title.lowercased())" : "Show \(title.lowercased())")
                        .font(DS.Font.meta)
                }
                .foregroundStyle(DS.Palette.textSecondary)
                .padding(.horizontal, 6)
                .frame(height: 20)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if open {
                ScrollView(.vertical) {
                    Text(body_)
                        .font(DS.Font.mono(10.5))
                        .foregroundStyle(DS.Palette.textSecondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .frame(maxHeight: 180)
                .background(RoundedRectangle(cornerRadius: 4).fill(DS.Palette.surfaceSecondary))
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(DS.Palette.divider, lineWidth: 1))
            }
        }
    }
}

// MARK: - Section label

struct SectionLabel: View {
    let title: String
    var trailing: String?

    var body: some View {
        HStack {
            Text(title)
                .font(DS.Font.bodyEmphasis)
                .foregroundStyle(DS.Palette.textSecondary)
            Spacer()
            if let trailing {
                Text(trailing)
                    .font(DS.Font.micro)
                    .monospacedDigit()
                    .foregroundStyle(DS.Palette.textTertiary)
            }
        }
    }
}

/// Compact label/value grid used by the inspector overview.
struct MetadataGrid: View {
    struct Row {
        let label: String
        let value: String?
        let mono: Bool
    }

    let rows: [Row]

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            ForEach(rows.indices, id: \.self) { index in
                let row = rows[index]
                GridRow {
                    Text(row.label)
                        .font(DS.Font.body)
                        .foregroundStyle(DS.Palette.textTertiary)
                    if let value = row.value {
                        Text(value)
                            .font(row.mono ? DS.Font.monoID : DS.Font.body)
                            .monospacedDigit()
                            .foregroundStyle(DS.Palette.textPrimary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .help(value)
                    } else {
                        Text("—")
                            .font(DS.Font.body)
                            .foregroundStyle(DS.Palette.textTertiary)
                    }
                }
            }
        }
    }
}
