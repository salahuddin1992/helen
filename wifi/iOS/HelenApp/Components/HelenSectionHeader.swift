import SwiftUI

/// A page section header with optional trailing action.
struct HelenSectionHeader: View {
    let title: LocalizedStringKey
    var caption: LocalizedStringKey? = nil
    var actionTitle: LocalizedStringKey? = nil
    var actionIcon: String? = nil
    var action: (() -> Void)? = nil

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(HelenFont.title3)
                    .foregroundStyle(theme.colors.textPrimary)
                if let caption {
                    Text(caption)
                        .font(HelenFont.footnote)
                        .foregroundStyle(theme.colors.textSecondary)
                }
            }
            Spacer(minLength: HelenSpace.md)
            if let actionTitle, let action {
                Button(action: action) {
                    HStack(spacing: 4) {
                        Text(actionTitle).font(HelenFont.subhead.weight(.semibold))
                        if let actionIcon { Image(systemName: actionIcon).font(.caption.weight(.semibold)) }
                    }
                    .foregroundStyle(theme.colors.accent)
                }
                .buttonStyle(.plain)
            }
        }
    }
}
