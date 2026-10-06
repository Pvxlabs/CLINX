import SwiftUI
import MarkdownUI

/// One reading style for native session responses and exact execution feedback.
/// Equatable keeps unchanged polling snapshots from re-parsing the transcript.
struct ActivityMarkdownView: View, Equatable {
    let text: String
    var prepared: MarkdownContent? = nil
    private var preferences = TypographyPreferences()

    static func == (lhs: Self, rhs: Self) -> Bool { lhs.text == rhs.text }

    var body: some View {
        Markdown(prepared ?? MarkdownContent(text))
            .markdownTheme(.activity(preferences.style))
            .markdownImageProvider(ActivityImagePlaceholder())
            .markdownInlineImageProvider(ActivityImagePlaceholder())
            .lineLimit(nil)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    static let readingWidth: CGFloat = 800
}

private extension Theme {
    static func activity(_ style: TypographyStyle) -> Theme { Theme.gitHub
        .text {
            FontFamily(.system(style.family.design))
            FontSize(style.bodySize)
            FontWeight(style.weight.fontWeight)
            ForegroundColor(DS.Palette.textPrimary)
            BackgroundColor(nil)
        }
        .code {
            FontFamily(.system(.monospaced))
            FontSize(style.monoSize)
            BackgroundColor(DS.Palette.textSecondary.opacity(0.14))
        }
        .link { ForegroundColor(DS.Palette.accent) }
        .strong { FontWeight(style.weight == .semibold || style.weight == .bold ? .bold : .semibold) }
        .heading1 { configuration in
            configuration.label
                .markdownTextStyle { FontSize(style.bodySize * 22 / 15); FontWeight(style.weight.resolve(.semibold)) }
                .relativeLineSpacing(.em(0.2))
                .markdownMargin(top: 24, bottom: 12)
        }
        .heading2 { configuration in
            configuration.label
                .markdownTextStyle { FontSize(style.bodySize * 19 / 15); FontWeight(style.weight.resolve(.semibold)) }
                .relativeLineSpacing(.em(0.2))
                .markdownMargin(top: 22, bottom: 10)
        }
        .heading3 { configuration in
            configuration.label
                .markdownTextStyle { FontSize(style.bodySize * 16 / 15); FontWeight(style.weight.resolve(.semibold)) }
                .markdownMargin(top: 20, bottom: 8)
        }
        .paragraph { configuration in
            configuration.label
                .fixedSize(horizontal: false, vertical: true)
                .lineSpacing(style.paragraphSpacing)
                .markdownMargin(top: 0, bottom: 16)
        }
        .codeBlock { configuration in
            ScrollView(.horizontal) {
                configuration.label
                    .markdownTextStyle { FontFamily(.system(.monospaced)); FontSize(style.monoSize); FontWeight(.regular) }
                    .fixedSize(horizontal: true, vertical: true)
                    .relativeLineSpacing(.em(0.25))
                    .padding(16)
            }
            .background(DS.Palette.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(DS.Palette.border))
            .markdownMargin(top: 0, bottom: 16)
        }
        .blockquote { configuration in
            configuration.label
                .markdownTextStyle { ForegroundColor(DS.Palette.textSecondary) }
                .padding(.leading, 16)
                .overlay(alignment: .leading) {
                    Rectangle().fill(DS.Palette.border).frame(width: 3)
                }
                .markdownMargin(top: 0, bottom: 16)
        }
        .table { configuration in
            configuration.label
                .markdownTableBorderStyle(.init(.insideHorizontalBorders, color: DS.Palette.border))
                .markdownTableBackgroundStyle(.clear)
                .markdownMargin(top: 0, bottom: 20)
        }
        .tableCell { configuration in
            configuration.label
                .markdownTextStyle {
                    if configuration.row == 0 { FontWeight(.semibold) }
                }
                .fixedSize(horizontal: false, vertical: true)
                .lineSpacing(style.paragraphSpacing)
                .padding(.vertical, 10)
                .padding(.horizontal, 12)
        }
        .thematicBreak {
            Rectangle().fill(DS.Palette.border).frame(height: 1)
                .markdownMargin(top: 20, bottom: 20)
        }
    }
}

// Feedback is text-only: parsing a transcript must not fetch embedded image URLs.
private struct ActivityImagePlaceholder: ImageProvider, InlineImageProvider {
    func makeImage(url: URL?) -> some View {
        Label("Image attachment", systemImage: "photo")
            .font(DS.Font.meta).foregroundStyle(DS.Palette.textSecondary)
    }
    func image(with url: URL, label: String) async throws -> Image {
        Image(systemName: "photo")
    }
}
