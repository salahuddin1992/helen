import SwiftUI

/// The premium hero block at the top of the profile screen.
///
/// A soft tinted halo behind the avatar — color is derived deterministically
/// from the contact's name so the same person gets the same hero each time.
/// Sits on `.ultraThinMaterial` so it reads beautifully on any wallpaper /
/// system appearance without picking arbitrary colors.
struct ProfileHeroView: View {
    let profile: ContactProfile
    var avatarSize: CGFloat = 144

    @Environment(\.theme) private var theme

    var body: some View {
        VStack(spacing: HelenSpace.md) {
            ZStack {
                haloGradient
                    .frame(width: avatarSize + 80, height: avatarSize + 80)
                    .blur(radius: 28)
                    .opacity(0.55)

                HelenAvatar(name: profile.displayName,
                            imageURL: profile.photoURL,
                            size: .xl,
                            presence: profile.presence)
                    .scaleEffect(avatarSize / HelenSize.avatarXl)
                    .shadow(color: .black.opacity(0.18), radius: 20, x: 0, y: 12)
            }
            .frame(height: avatarSize + 24)
            .accessibilityHidden(true)

            VStack(spacing: HelenSpace.xs) {
                Text(profile.displayName)
                    .font(HelenFont.title.weight(.semibold))
                    .foregroundStyle(theme.colors.textPrimary)
                    .multilineTextAlignment(.center)

                if let metadata = roleLine {
                    Text(metadata)
                        .font(HelenFont.subhead)
                        .foregroundStyle(theme.colors.textSecondary)
                        .multilineTextAlignment(.center)
                }
            }
            .accessibilityElement(children: .combine)

            HelenBadge(text: presenceLabel,
                       icon: "circle.fill",
                       tone: presenceTone)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, HelenSpace.xl)
        .padding(.bottom, HelenSpace.lg)
        .padding(.horizontal, HelenSpace.pageH)
        .background {
            // Tinted material backdrop — lifts the hero off the page
            // background without committing to a specific accent.
            Rectangle()
                .fill(.ultraThinMaterial)
                .overlay(
                    LinearGradient(
                        colors: [haloColor.opacity(0.15), .clear],
                        startPoint: .top, endPoint: .bottom
                    )
                )
                .ignoresSafeArea(edges: .top)
        }
    }

    // MARK: – derived

    private var roleLine: String? {
        switch (profile.title, profile.company) {
        case let (t?, c?): return "\(t) · \(c)"
        case let (t?, nil): return t
        case let (nil, c?): return c
        default:            return nil
        }
    }

    private var presenceLabel: String {
        switch profile.presence {
        case .online:  return NSLocalizedString("Online now", comment: "")
        case .away:    return NSLocalizedString("Away",       comment: "")
        case .offline: return NSLocalizedString("Offline",    comment: "")
        }
    }
    private var presenceTone: HelenBadge.Tone {
        switch profile.presence {
        case .online:  return .success
        case .away:    return .warning
        case .offline: return .neutral
        }
    }

    private var haloColor: Color {
        AvatarPalette.color(for: profile.displayName)
    }

    private var haloGradient: some View {
        RadialGradient(
            colors: [haloColor.opacity(0.85), haloColor.opacity(0)],
            center: .center,
            startRadius: 0,
            endRadius: avatarSize
        )
    }
}
