import SwiftUI

/// Single source of truth for "name → avatar color" mapping.
///
/// Hash logic was duplicated in `HelenAvatar` and `ProfileHeroView` — both
/// now go through this enum. Same name always returns the same palette
/// entry; identical input on different surfaces means identical color.
enum AvatarPalette {
    /// Pick a stable color for a given display name.
    static func color(for name: String) -> Color {
        guard !name.isEmpty else { return HelenColor.avatarPalette[0] }
        var hash = 5381
        for ch in name.unicodeScalars {
            hash = ((hash << 5) &+ hash) &+ Int(ch.value)
        }
        return HelenColor.avatarPalette[abs(hash) % HelenColor.avatarPalette.count]
    }

    /// Two-letter initials extracted the way humans expect — first letters
    /// of the first two whitespace-separated tokens, uppercased.
    static func initials(for name: String) -> String {
        let parts = name
            .split(whereSeparator: { $0.isWhitespace || $0 == "@" || $0 == "." || $0 == "_" })
            .prefix(2)
        return parts.compactMap { $0.first }.map { String($0).uppercased() }.joined()
    }
}
