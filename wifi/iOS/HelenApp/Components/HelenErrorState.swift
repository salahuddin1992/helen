import SwiftUI

/// Centered error view with an optional retry button. Mirrors the visual
/// shape of `HelenEmptyState` so flows feel consistent — but the icon
/// circle is tinted with `danger` instead of `accent`.
struct HelenErrorState: View {
    var symbol: String = "exclamationmark.triangle.fill"
    var title: LocalizedStringKey = "Something went wrong"
    var message: LocalizedStringKey
    var retry: (() -> Void)? = nil

    @Environment(\.theme) private var theme

    var body: some View {
        VStack(spacing: HelenSpace.lg) {
            ZStack {
                Circle()
                    .fill(theme.colors.danger.opacity(0.12))
                    .frame(width: 96, height: 96)
                Image(systemName: symbol)
                    .font(.system(size: 36, weight: .semibold))
                    .foregroundStyle(theme.colors.danger)
                    .accessibilityHidden(true)
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
                    .lineLimit(5)
            }
            .padding(.horizontal, HelenSpace.lg)

            if let retry {
                HelenButton(
                    title: "Try again",
                    icon: "arrow.clockwise",
                    variant: .secondary,
                    fullWidth: false,
                    action: retry
                )
                .padding(.top, HelenSpace.sm)
            }
        }
        .padding(.vertical, HelenSpace.xxxl)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}
