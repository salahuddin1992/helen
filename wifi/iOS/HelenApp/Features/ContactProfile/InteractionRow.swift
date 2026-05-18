import SwiftUI

/// One row in the "Recent activity" section — leading icon disc, summary,
/// trailing relative date. Same visual rhythm as `HelenListRow` but
/// dedicated to activity since the symbology is opinionated here.
struct InteractionRow: View {
    let interaction: Interaction

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.md) {
            ZStack {
                RoundedRectangle(cornerRadius: HelenRadius.sm, style: .continuous)
                    .fill(iconBg)
                Image(systemName: iconName)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(iconFg)
            }
            .frame(width: 32, height: 32)

            Text(interaction.summary)
                .font(HelenFont.subhead)
                .foregroundStyle(theme.colors.textPrimary)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(relativeDate)
                .font(HelenFont.caption.monospacedDigit())
                .foregroundStyle(theme.colors.textTertiary)
        }
        .padding(.vertical, HelenSpace.sm)
        .padding(.horizontal, HelenSpace.lg)
        .accessibilityElement(children: .combine)
    }

    // MARK: – derived

    private var iconName: String {
        switch interaction.kind {
        case .callIncoming: return "arrow.down.left"
        case .callOutgoing: return "arrow.up.right"
        case .callMissed:   return "phone.down.fill"
        case .message:      return "bubble.left.fill"
        case .email:        return "envelope.fill"
        }
    }
    private var iconFg: Color {
        switch interaction.kind {
        case .callMissed: return theme.colors.danger
        case .message:    return theme.colors.accent
        case .email:      return theme.colors.accent
        default:          return theme.colors.success
        }
    }
    private var iconBg: Color {
        switch interaction.kind {
        case .callMissed: return theme.colors.danger.opacity(0.12)
        case .message,
             .email:      return theme.colors.accentMuted
        default:          return theme.colors.success.opacity(0.14)
        }
    }
    private var relativeDate: String {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        f.locale = .current
        return f.localizedString(for: interaction.date, relativeTo: .now)
    }
}
