import SwiftUI

/// A small status pill. Keep usage rare — badges easily clutter a UI.
struct HelenBadge: View {

    enum Tone { case neutral, accent, success, warning, danger }

    let text: String
    var icon: String? = nil
    var tone: Tone = .neutral

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: 4) {
            if let icon {
                Image(systemName: icon).font(.caption2.weight(.semibold))
            }
            Text(text)
        }
        .font(HelenFont.caption2.weight(.semibold))
        .foregroundStyle(foreground)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(background)
        .clipShape(Capsule())
    }

    private var foreground: Color {
        switch tone {
        case .neutral: return theme.colors.textSecondary
        case .accent:  return theme.colors.accent
        case .success: return theme.colors.success
        case .warning: return theme.colors.warning
        case .danger:  return theme.colors.danger
        }
    }
    private var background: Color {
        switch tone {
        case .neutral: return theme.colors.surfaceAlt
        case .accent:  return theme.colors.accentMuted
        case .success: return theme.colors.success.opacity(0.12)
        case .warning: return theme.colors.warning.opacity(0.15)
        case .danger:  return theme.colors.danger.opacity(0.12)
        }
    }
}

/// An unread count chip. Renders nothing when count is 0.
struct HelenUnreadDot: View {
    let count: Int
    @Environment(\.theme) private var theme

    var body: some View {
        if count > 0 {
            Text(count > 99 ? "99+" : String(count))
                .font(HelenFont.caption2.weight(.bold))
                .foregroundStyle(theme.colors.textOnAccent)
                .padding(.horizontal, count > 9 ? 6 : 0)
                .frame(minWidth: 20, minHeight: 20)
                .background(theme.colors.accent)
                .clipShape(Capsule())
                .accessibilityLabel("\(count) unread")
        }
    }
}
