import SwiftUI

/// Circular avatar that falls back to colored initials.
///
/// Background color is derived deterministically from the name so the same
/// person always gets the same color, no manual assignment needed.
struct Avatar: View {

    enum Size {
        case sm, md, lg, xl
        var dimension: CGFloat {
            switch self {
            case .sm: return AppSize.avatarSm
            case .md: return AppSize.avatarMd
            case .lg: return AppSize.avatarLg
            case .xl: return AppSize.avatarXl
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

    enum Presence { case online, away, offline }

    let name: String
    var imageURL: URL? = nil
    var size: Size = .md
    var presence: Presence? = nil

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            base
                .frame(width: size.dimension, height: size.dimension)
                .clipShape(Circle())
                .overlay(
                    Circle().strokeBorder(AppColors.border.opacity(0.5), lineWidth: 0.5)
                )

            if let presence {
                Circle()
                    .fill(presenceColor(presence))
                    .frame(width: size.dimension * 0.28, height: size.dimension * 0.28)
                    .overlay(Circle().strokeBorder(AppColors.background, lineWidth: 2))
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
                case .success(let img): img.resizable().scaledToFill()
                default:                initials
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

    // MARK: – Helpers

    private var initialsString: String {
        let parts = name
            .split(whereSeparator: { $0.isWhitespace || $0 == "@" || $0 == "." || $0 == "_" })
            .prefix(2)
        return parts.compactMap { $0.first }.map { String($0).uppercased() }.joined()
    }

    private var color: Color {
        guard !name.isEmpty else { return AppColors.avatarPalette[0] }
        var hash = 5381
        for ch in name.unicodeScalars { hash = ((hash << 5) &+ hash) &+ Int(ch.value) }
        return AppColors.avatarPalette[abs(hash) % AppColors.avatarPalette.count]
    }

    private func presenceColor(_ p: Presence) -> Color {
        switch p {
        case .online:  return AppColors.success
        case .away:    return AppColors.warning
        case .offline: return AppColors.textTertiary
        }
    }
}
