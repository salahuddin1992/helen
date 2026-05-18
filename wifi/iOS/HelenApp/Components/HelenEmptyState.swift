import SwiftUI

/// A centered illustration + title + description + optional CTA.
/// Use for "no contacts", "no chats", search "no results", etc.
struct HelenEmptyState: View {
    let symbol: String
    let title: LocalizedStringKey
    let message: LocalizedStringKey
    var actionTitle: LocalizedStringKey? = nil
    var action: (() -> Void)? = nil

    @Environment(\.theme) private var theme

    var body: some View {
        VStack(spacing: HelenSpace.lg) {
            ZStack {
                Circle()
                    .fill(theme.colors.accentMuted)
                    .frame(width: 96, height: 96)
                Image(systemName: symbol)
                    .font(.system(size: 38, weight: .semibold))
                    .foregroundStyle(theme.colors.accent)
            }

            VStack(spacing: HelenSpace.sm) {
                Text(title)
                    .font(HelenFont.title3)
                    .foregroundStyle(theme.colors.textPrimary)
                    .multilineTextAlignment(.center)
                Text(message)
                    .font(HelenFont.subhead)
                    .foregroundStyle(theme.colors.textSecondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
            }
            .padding(.horizontal, HelenSpace.lg)

            if let actionTitle, let action {
                HelenButton(title: actionTitle, variant: .primary, fullWidth: false, action: action)
                    .padding(.top, HelenSpace.sm)
            }
        }
        .padding(.vertical, HelenSpace.xxxl)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}
