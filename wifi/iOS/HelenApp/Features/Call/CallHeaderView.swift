import SwiftUI

/// Top bar of the call screen — app name on the left, user avatar on the
/// right, connection status pill underneath. Kept tightly contained so it
/// can drop in anywhere.
struct CallHeaderView: View {
    let userName: String
    let isConnected: Bool

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(alignment: .center, spacing: HelenSpace.md) {
            VStack(alignment: .leading, spacing: HelenSpace.xs) {
                Text("Helen")
                    .font(HelenFont.display)
                    .foregroundStyle(theme.colors.textPrimary)
                ConnectionPill(isConnected: isConnected)
            }
            Spacer()
            UserBubble(name: userName)
        }
        .padding(.horizontal, HelenSpace.pageH)
        .padding(.top,        HelenSpace.lg)
        .padding(.bottom,     HelenSpace.md)
    }
}

private struct ConnectionPill: View {
    let isConnected: Bool
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.xs) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
                .overlay(
                    Circle().stroke(color.opacity(0.3), lineWidth: 4)
                )
            Text(isConnected ? "Connected on Wi-Fi" : "Offline")
                .font(HelenFont.caption.weight(.medium))
                .foregroundStyle(theme.colors.textSecondary)
        }
        .padding(.horizontal, HelenSpace.sm)
        .padding(.vertical,   4)
        .background(theme.colors.surfaceAlt)
        .clipShape(Capsule())
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        isConnected ? theme.colors.success : theme.colors.textTertiary
    }
}

private struct UserBubble: View {
    let name: String
    @Environment(\.theme) private var theme

    var body: some View {
        Button {} label: {
            ZStack {
                Circle()
                    .fill(theme.colors.surface)
                    .helenShadow(.sm)
                HelenAvatar(name: name, size: .md, presence: .online)
                    .padding(2)
            }
            .frame(width: HelenSize.avatarMd + 6, height: HelenSize.avatarMd + 6)
        }
        .buttonStyle(PressableScaleStyle())
        .accessibilityLabel("Open profile for \(name)")
    }
}
