import SwiftUI

/// Horizontal pill of 4 quick-action buttons — Call, Message, FaceTime, Email.
/// Single source of truth for action chrome on the profile screen.
struct ProfileActionRow: View {
    let canEmail: Bool
    var onCall:     () -> Void = {}
    var onMessage:  () -> Void = {}
    var onFaceTime: () -> Void = {}
    var onEmail:    () -> Void = {}

    var body: some View {
        HStack(spacing: HelenSpace.sm) {
            ProfileActionButton(icon: "phone.fill",         title: "Call",     action: onCall)
            ProfileActionButton(icon: "message.fill",       title: "Message",  action: onMessage)
            ProfileActionButton(icon: "video.fill",         title: "FaceTime", action: onFaceTime)
            ProfileActionButton(icon: "envelope.fill",      title: "Email",
                                isEnabled: canEmail,        action: onEmail)
        }
    }
}

/// One action — a rounded square with icon on top, label below.
/// Designed to be repeatable; the layout dictates the metrics.
struct ProfileActionButton: View {
    let icon: String
    let title: LocalizedStringKey
    var isEnabled: Bool = true
    let action: () -> Void

    @Environment(\.theme) private var theme

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            action()
        } label: {
            VStack(spacing: HelenSpace.xs) {
                Image(systemName: icon)
                    .font(.body.weight(.semibold))
                Text(title)
                    .font(HelenFont.caption.weight(.semibold))
            }
            .foregroundStyle(isEnabled ? theme.colors.accent
                                       : theme.colors.textTertiary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, HelenSpace.md)
            .background(
                RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous)
                    .fill(.thinMaterial)
            )
            .overlay(
                RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous)
                    .strokeBorder(theme.colors.border, lineWidth: 0.5)
            )
            .opacity(isEnabled ? 1 : 0.55)
        }
        .buttonStyle(PressableScaleStyle(scale: 0.95))
        .disabled(!isEnabled)
        .accessibilityLabel(title)
    }
}

#Preview {
    ProfileActionRow(canEmail: true)
        .padding()
        .background(HelenColor.background)
}
