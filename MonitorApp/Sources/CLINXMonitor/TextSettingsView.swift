import SwiftUI

struct TextSettingsView: View {
    private var preferences = TypographyPreferences()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("文字").font(DS.Font.bodyEmphasis)
                Spacer()
                Button("恢复默认文字设置") { preferences.reset() }
                    .buttonStyle(.plain).foregroundStyle(DS.Palette.accent)
            }

            VStack(spacing: 12) {
                HStack {
                    Text("字体")
                    Spacer()
                    Picker("字体", selection: preferences.$family) {
                        ForEach(TextFontFamily.allCases) { Text($0.label).tag($0) }
                    }
                    .labelsHidden().frame(width: 160)
                }
                HStack {
                    Text("字重")
                    Spacer()
                    Picker("字重", selection: preferences.$weight) {
                        ForEach(TextFontWeight.allCases) { Text($0.label).tag($0) }
                    }
                    .labelsHidden().frame(width: 160)
                }
                sizeControl("界面字号", value: preferences.$interfaceSize, range: 10...18)
                sizeControl("正文字号", value: preferences.$contentSize, range: 12...24)
                sizeControl("代码字号", value: preferences.$codeSize, range: 10...20)
                sizeControl("正文行距", value: preferences.$lineSpacing, range: 0...10)
            }

            Text("界面字号调整侧栏、任务列表和信息标签；正文字号和行距调整 Activity 回复。代码始终使用等宽字体。修改即时生效并保存在本机。")
                .font(DS.Font.micro).foregroundStyle(DS.Palette.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("界面预览").font(DS.Font.rowTitle)
                        Spacer()
                        Text("已连接 · 2m ago").font(DS.Font.meta)
                    }
                    Rectangle().fill(DS.Palette.border).frame(height: 1)
                    ActivityMarkdownView(text: """
                    **会话反馈预览**

                    正文与界面字号分别设置。段落保留留白，支持 **强调文字** 和 `行内代码`。

                    ```swift
                    let status = "PASS"
                    print(status)
                    ```
                    """).equatable()
                }
                .padding(16)
            }
            .frame(height: 190)
            .background(RoundedRectangle(cornerRadius: 8).fill(DS.Palette.surfaceSecondary))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(DS.Palette.border))
        }
        .font(DS.Font.body)
        .foregroundStyle(DS.Palette.textPrimary)
    }

    private func sizeControl(_ title: String, value: Binding<Double>, range: ClosedRange<Double>) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text("\(value.wrappedValue.formatted(.number.precision(.fractionLength(0)))) pt").monospacedDigit()
                .frame(minWidth: 45, alignment: .trailing)
            Stepper(title, value: value, in: range, step: 1)
                .labelsHidden().fixedSize().accessibilityLabel(title)
        }
    }
}
