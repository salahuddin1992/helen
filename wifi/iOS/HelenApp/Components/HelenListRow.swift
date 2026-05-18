import SwiftUI

/// A reusable list row — leading slot, two-line text, trailing slot.
/// Use this everywhere you'd otherwise build a `HStack` from scratch.
struct HelenListRow<Leading: View, Trailing: View>: View {

    let title: String
    var subtitle: String? = nil
    var titleStyle: Font = HelenFont.bodyMed
    var subtitleStyle: Font = HelenFont.footnote
    var truncate: Bool = true
    @ViewBuilder var leading:  () -> Leading
    @ViewBuilder var trailing: () -> Trailing
    var onTap: (() -> Void)? = nil

    @Environment(\.theme) private var theme

    var body: some View {
        let row = HStack(spacing: HelenSpace.md) {
            leading()
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(titleStyle)
                    .foregroundStyle(theme.colors.textPrimary)
                    .lineLimit(truncate ? 1 : nil)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(subtitleStyle)
                        .foregroundStyle(theme.colors.textSecondary)
                        .lineLimit(truncate ? 1 : nil)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            trailing()
        }
        .padding(.horizontal, HelenSpace.lg)
        .padding(.vertical, HelenSpace.md)
        .frame(minHeight: HelenSize.minTap + HelenSpace.sm)
        .contentShape(Rectangle())

        if let onTap {
            Button(action: onTap) { row }
                .buttonStyle(PressableRowStyle())
        } else {
            row
        }
    }
}

extension HelenListRow where Trailing == EmptyView {
    init(
        title: String,
        subtitle: String? = nil,
        titleStyle: Font = HelenFont.bodyMed,
        subtitleStyle: Font = HelenFont.footnote,
        truncate: Bool = true,
        @ViewBuilder leading: @escaping () -> Leading,
        onTap: (() -> Void)? = nil
    ) {
        self.title = title
        self.subtitle = subtitle
        self.titleStyle = titleStyle
        self.subtitleStyle = subtitleStyle
        self.truncate = truncate
        self.leading = leading
        self.trailing = { EmptyView() }
        self.onTap = onTap
    }
}

private struct PressableRowStyle: ButtonStyle {
    @Environment(\.theme) private var theme
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed
                        ? theme.colors.surfaceAlt
                        : Color.clear)
            .animation(HelenMotion.quick, value: configuration.isPressed)
    }
}
