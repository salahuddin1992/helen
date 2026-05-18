import SwiftUI

/// A small pill — used for "Online", "Connected on Wi-Fi", "Beta", etc.
/// Keep usage rare. Badges crowd a UI fast.
struct StatusBadge: View {

    enum Tone { case neutral, accent, success, warning, danger }

    let text: String
    var icon: String? = nil
    var tone: Tone = .neutral

    var body: some View {
        HStack(spacing: 4) {
            if let icon {
                Image(systemName: icon)
                    .font(.caption2.weight(.semibold))
                    .accessibilityHidden(true)
            }
            Text(text)
        }
        .font(AppTypography.caption2.weight(.semibold))
        .foregroundStyle(foreground)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(background)
        .clipShape(Capsule())
        .accessibilityElement(children: .combine)
    }

    private var foreground: Color {
        switch tone {
        case .neutral: return AppColors.textSecondary
        case .accent:  return AppColors.primary
        case .success: return AppColors.success
        case .warning: return AppColors.warning
        case .danger:  return AppColors.danger
        }
    }
    private var background: Color {
        switch tone {
        case .neutral: return AppColors.surfaceAlt
        case .accent:  return AppColors.primaryMuted
        case .success: return AppColors.success.opacity(0.12)
        case .warning: return AppColors.warning.opacity(0.15)
        case .danger:  return AppColors.danger.opacity(0.12)
        }
    }
}

/// An unread-count chip. Renders nothing when count is 0.
struct UnreadDot: View {
    let count: Int

    var body: some View {
        if count > 0 {
            Text(count > 99 ? "99+" : String(count))
                .font(AppTypography.caption2.weight(.bold))
                .foregroundStyle(AppColors.textInverse)
                .padding(.horizontal, count > 9 ? 6 : 0)
                .frame(minWidth: 20, minHeight: 20)
                .background(AppColors.primary)
                .clipShape(Capsule())
                .accessibilityLabel("\(count) unread")
        }
    }
}
