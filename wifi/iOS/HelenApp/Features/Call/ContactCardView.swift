import SwiftUI

/// One contact card. Tapping the card opens detail; tapping the trailing
/// circular button starts a call. Both have full press feedback + haptics.
struct ContactCardView: View {
    let contact: CallContact
    var onOpen: () -> Void = {}
    var onCall: () -> Void = {}

    @Environment(\.theme) private var theme

    var body: some View {
        Button(action: onOpen) {
            HStack(spacing: HelenSpace.md) {
                HelenAvatar(name: contact.name, size: .lg, presence: contact.presence)

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: HelenSpace.xs) {
                        Text(contact.name)
                            .font(HelenFont.bodyEmph)
                            .foregroundStyle(theme.colors.textPrimary)
                            .lineLimit(1)
                        if contact.isFavorite {
                            Image(systemName: "star.fill")
                                .font(.caption2)
                                .foregroundStyle(theme.colors.warning)
                                .accessibilityLabel("Favorite")
                        }
                    }
                    Text(contact.phone)
                        .font(HelenFont.subhead.monospacedDigit())
                        .foregroundStyle(theme.colors.textSecondary)
                        .lineLimit(1)
                    PresenceLabel(presence: contact.presence)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                CallButton(action: onCall)
            }
            .padding(HelenSpace.md)
            .helenCardSurface(cornerRadius: HelenRadius.xl, elevation: .sm)
            .contentShape(RoundedRectangle(cornerRadius: HelenRadius.xl))
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

private struct PresenceLabel: View {
    let presence: HelenAvatar.Presence
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(dotColor).frame(width: 6, height: 6)
            Text(label)
                .font(HelenFont.caption.weight(.medium))
                .foregroundStyle(theme.colors.textSecondary)
        }
    }

    private var dotColor: Color {
        switch presence {
        case .online:  return theme.colors.success
        case .away:    return theme.colors.warning
        case .offline: return theme.colors.textTertiary
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

private struct CallButton: View {
    let action: () -> Void
    @Environment(\.theme) private var theme

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            action()
        } label: {
            ZStack {
                Circle().fill(theme.colors.success.opacity(0.12))
                Image(systemName: "phone.fill")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(theme.colors.success)
            }
            .frame(width: 44, height: 44)
        }
        .buttonStyle(PressableScaleStyle(scale: 0.92))
        .accessibilityLabel("Call")
    }
}

#Preview {
    VStack(spacing: HelenSpace.md) {
        ContactCardView(contact: CallContact.samples[0])
        ContactCardView(contact: CallContact.samples[2])
        ContactCardView(contact: CallContact.samples[3])
    }
    .padding()
    .background(HelenColor.background)
}
