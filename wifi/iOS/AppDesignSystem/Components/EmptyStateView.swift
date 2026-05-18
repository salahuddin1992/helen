import SwiftUI

/// Centered illustration + title + message + optional CTA.
/// Use for "no contacts", "no recents", "no search results".
struct EmptyStateView: View {
    let symbol: String
    let title: LocalizedStringKey
    let message: LocalizedStringKey
    var actionTitle: LocalizedStringKey? = nil
    var action: (() -> Void)? = nil

    var body: some View {
        VStack(spacing: AppSpacing.lg) {
            ZStack {
                Circle()
                    .fill(AppColors.primaryMuted)
                    .frame(width: 96, height: 96)
                Image(systemName: symbol)
                    .font(.system(size: 36, weight: .semibold))
                    .foregroundStyle(AppColors.primary)
                    .accessibilityHidden(true)
            }

            VStack(spacing: AppSpacing.sm) {
                Text(title)
                    .font(AppTypography.title3)
                    .foregroundStyle(AppColors.textPrimary)
                    .multilineTextAlignment(.center)
                Text(message)
                    .font(AppTypography.subhead)
                    .foregroundStyle(AppColors.textSecondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(5)
            }
            .padding(.horizontal, AppSpacing.lg)

            if let actionTitle, let action {
                PrimaryButton(title: actionTitle,
                              variant: .primary,
                              fullWidth: false,
                              action: action)
                    .padding(.top, AppSpacing.sm)
            }
        }
        .padding(.vertical, AppSpacing.xxxl)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}
