import SwiftUI

/// A circular avatar that falls back to colored initials.
///
/// The background color is derived deterministically from the name so the
/// same person always gets the same color, without manual assignment.
struct HelenAvatar: View {

    enum Size {
        case sm, md, lg, xl
        var dimension: CGFloat {
            switch self {
            case .sm: return HelenSize.avatarSm
            case .md: return HelenSize.avatarMd
            case .lg: return HelenSize.avatarLg
            case .xl: return HelenSize.avatarXl
            }
        }
        var fontSize: CGFloat {
            switch self {
            case .sm: return 13
            case .md: return 17
            case .lg: return 22
            case .xl: return 34
            }
        }
    }

    let name: String
    var imageURL: URL? = nil
    var size: Size = .md
    var presence: Presence? = nil

    enum Presence { case online, away, offline }

    @Environment(\.theme) private var theme

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            base
                .frame(width: size.dimension, height: size.dimension)
                .clipShape(Circle())
                .overlay(
                    Circle().strokeBorder(theme.colors.border.opacity(0.5), lineWidth: 0.5)
                )

            if let presence {
                Circle()
                    .fill(presenceColor(presence))
                    .frame(width: size.dimension * 0.28, height: size.dimension * 0.28)
                    .overlay(Circle().strokeBorder(theme.colors.background, lineWidth: 2))
                    .accessibilityHidden(true)
            }
        }
        .accessibilityLabel(Text("Avatar for \(name)"))
    }

    @ViewBuilder
    private var base: some View {
        if let imageURL {
            AsyncImage(url: imageURL) { phase in
                switch phase {
                case .success(let img):
                    img.resizable().scaledToFill()
                default:
                    initials
                }
            }
        } else {
            initials
        }
    }

    private var initials: some View {
        ZStack {
            color
            Text(initialsString)
                .font(.system(size: size.fontSize, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
        }
    }

    private var initialsString: String { AvatarPalette.initials(for: name) }
    private var color:           Color { AvatarPalette.color(for: name) }

    private func presenceColor(_ p: Presence) -> Color {
        switch p {
        case .online:  return theme.colors.success
        case .away:    return theme.colors.warning
        case .offline: return theme.colors.textTertiary
        }
    }
}

#Preview {
    HStack(spacing: 24) {
        HelenAvatar(name: "Yousef Salah",   size: .sm, presence: .online)
        HelenAvatar(name: "Helen Server",   size: .md, presence: .away)
        HelenAvatar(name: "Maya Khalil",    size: .lg, presence: .offline)
        HelenAvatar(name: "Ahmed",          size: .xl, presence: .online)
    }
    .padding(40)
    .background(HelenColor.background)
}
