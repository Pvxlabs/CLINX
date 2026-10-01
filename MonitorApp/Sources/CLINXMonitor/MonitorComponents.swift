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

struct StageBadge: View {
    let stage: String
    var tone: Tone = .neutral

    enum Tone { case neutral, warn, error }

    private var color: Color {
        switch tone {
        case .neutral: return DS.Palette.textSecondary
        case .warn: return DS.Palette.warning
        case .error: return DS.Palette.error
        }
    }

    var body: some View {
        Text(stage)
            .font(DS.Font.monoStage)
            .foregroundStyle(color)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.horizontal, 6)
            .frame(height: 18)
            .background(RoundedRectangle(cornerRadius: DS.Metric.controlRadius).fill(DS.Palette.surface))
            .overlay(RoundedRectangle(cornerRadius: DS.Metric.controlRadius)
                .strokeBorder(color.opacity(0.35), lineWidth: 1))
    }
}

// MARK: - Environment badge

struct EnvironmentBadge: View {
    let synthetic: Bool

    var body: some View {
        if synthetic {
            HStack(spacing: 6) {
                Image(systemName: "flask")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.white)
                Text("SYNTHETIC DATA")
                    .font(DS.Font.mono(10, weight: .semibold))
                    .tracking(0.6)
                    .foregroundStyle(.white)
            }
            .padding(.horizontal, 8)
            .frame(height: 22)
            .background(RoundedRectangle(cornerRadius: 6).fill(DS.Palette.synthetic))
            .overlay(HatchPattern().stroke(Color.white.opacity(0.14), lineWidth: 5).clipShape(RoundedRectangle(cornerRadius: 6)))
            .help("Synthetic acceptance data — not P620 live")
        } else {
            HStack(spacing: 6) {
                Circle().fill(DS.Palette.success).frame(width: 6, height: 6)
                Text("P620").font(DS.Font.mono(10, weight: .semibold)).tracking(0.4)
                    .foregroundStyle(DS.Palette.textPrimary)
                Text("LIVE").font(DS.Font.mono(10, weight: .semibold)).tracking(0.4)
                    .foregroundStyle(DS.Palette.textTertiary)
            }
            .padding(.horizontal, 8)
            .frame(height: 22)
            .background(RoundedRectangle(cornerRadius: 6).fill(DS.Palette.surface))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(DS.Palette.border, lineWidth: 1))
            .help("Live P620 Observer data")
        }
    }
}

/// 45° hatch used by the synthetic badge.
struct HatchPattern: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let step: CGFloat = 10
        var x = -rect.height
        while x < rect.width + rect.height {
            path.move(to: CGPoint(x: x, y: rect.maxY))
            path.addLine(to: CGPoint(x: x + rect.height, y: rect.minY))
            x += step
        }
        return path
    }
}

// MARK: - Connection cluster

struct ConnectionCluster: View {
    let connection: ConnectionState7
    let authority: AuthorityState
    let sync: String
    /// Narrow windows keep connection · authority and drop the last-sync segment so the
    /// search field and the toolbar buttons stay visible instead of overflowing.
    var compact = false

    var body: some View {
        HStack(spacing: 0) {
            HStack(spacing: 6) {
                indicator
                Text(connection.label)
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(DS.Palette.textPrimary)
            }
            .padding(.horizontal, 8)

            if !compact {
                Rectangle().fill(DS.Palette.border).frame(width: 1, height: 12)

                Text(authority.label)
                    .font(DS.Font.mono(10, weight: .semibold))
                    .tracking(0.5)
                    .foregroundStyle(authority.color)
                    .padding(.horizontal, 8)
            }

            if !compact {
                Rectangle().fill(DS.Palette.border).frame(width: 1, height: 12)

                Text(sync)
                    .font(DS.Font.meta)
                    .monospacedDigit()
                    .foregroundStyle(DS.Palette.textSecondary)
                    .padding(.horizontal, 8)
            }
        }
        .frame(height: 26)
        .fixedSize()
        .background(RoundedRectangle(cornerRadius: 7).fill(DS.Palette.surface))
        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(DS.Palette.border, lineWidth: 1))
        .help("Observer \(connection.label.lowercased()) · authority \(authority.label.lowercased()) · last sync \(sync)")
    }

    @ViewBuilder private var indicator: some View {
        switch connection {
        case .connected: Circle().fill(connection.color).frame(width: 7, height: 7)
        case .degraded: Rectangle().fill(connection.color).frame(width: 7, height: 7).rotationEffect(.degrees(45))
        case .offline:
            Rectangle().strokeBorder(connection.color, lineWidth: 1.5).frame(width: 7, height: 7)
        }
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
