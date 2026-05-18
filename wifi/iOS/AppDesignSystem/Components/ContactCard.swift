import SwiftUI

/// Contact list-item card — avatar, name, phone, presence, quick-call.
/// Tapping the card body opens the contact; tapping the trailing pill
/// starts a call. Both callbacks are independent.
struct ContactCard: View {

    struct Model: Identifiable, Hashable {
        let id: String
        var name: String
        var phone: String
        var presence: Avatar.Presence
        var avatarURL: URL? = nil
        var isFavorite: Bool = false
    }

    let contact: Model
    var onOpen: () -> Void = {}
    var onCall: () -> Void = {}

    var body: some View {
        Button(action: onOpen) {
            HStack(spacing: AppSpacing.md) {
                Avatar(
                    name: contact.name,
                    imageURL: contact.avatarURL,
                    size: .lg,
                    presence: contact.presence
                )

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: AppSpacing.xs) {
                        Text(contact.name)
                            .font(AppTypography.bodyEmph)
                            .foregroundStyle(AppColors.textPrimary)
                            .lineLimit(1)
                        if contact.isFavorite {
                            Image(systemName: "star.fill")
                                .font(.caption2)
                                .foregroundStyle(AppColors.warning)
                                .accessibilityLabel("Favorite")
                        }
                    }
                    Text(contact.phone)
                        .font(AppTypography.subhead.monospacedDigit())
                        .foregroundStyle(AppColors.textSecondary)
                        .lineLimit(1)
                    PresenceLine(presence: contact.presence)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                CallPill(action: onCall)
            }
            .padding(AppSpacing.md)
            .appCardSurface(cornerRadius: AppRadius.xl, elevation: .sm)
            .contentShape(RoundedRectangle(cornerRadius: AppRadius.xl))
        }
        .buttonStyle(PressableScaleStyle(scale: 0.98))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(contact.name), \(contact.phone), \(presenceText)")
        .accessibilityAddTraits(.isButton)
    }

    private var presenceText: String {
        switch contact.presence {
        case .online:  return "online"
        case .away:    return "away"
        case .offline: return "offline"
        }
    }
}

// MARK: – sub-views

private struct PresenceLine: View {
    let presence: Avatar.Presence

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(dotColor).frame(width: 6, height: 6)
            Text(label)
                .font(AppTypography.caption.weight(.medium))
                .foregroundStyle(AppColors.textSecondary)
        }
    }

    private var dotColor: Color {
        switch presence {
        case .online:  return AppColors.success
        case .away:    return AppColors.warning
        case .offline: return AppColors.textTertiary
        }
    }
    private var label: LocalizedStringKey {
        switch presence {
        case .online:  return "Online"
        case .away:    return "Away"
        case .offline: return "Offline"
        }
    }
}

private struct CallPill: View {
    let action: () -> Void
    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            action()
        } label: {
            ZStack {
                Circle().fill(AppColors.success.opacity(0.12))
                Image(systemName: "phone.fill")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(AppColors.success)
            }
            .frame(width: 44, height: 44)
        }
        .buttonStyle(PressableScaleStyle(scale: 0.92))
        .accessibilityLabel("Call \(text)")
    }
    private var text: String { "" } // accessibility-only stub
}
