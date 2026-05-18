import SwiftUI

/// Semantic color tokens for the calling app.
///
/// Every token is *role-named* (background, surface, textPrimary, …) — never
/// hue-named (blue, gray). That's what lets the same token resolve to a
/// different shade in dark mode without the call site knowing.
///
/// Add a token before adding a one-off color in feature code.
enum AppColors {

    // MARK: – Brand
    static let primary         = adaptive(light: 0x0A6CFF, dark: 0x4F90FF)
    static let primaryMuted    = adaptive(light: 0xE6F0FF, dark: 0x16294F)
    static let primaryPressed  = adaptive(light: 0x0856CC, dark: 0x3B7AE6)

    // MARK: – Surfaces
    /// Sits behind the whole screen.
    static let background      = adaptive(light: 0xF6F6F8, dark: 0x000000)
    /// Cards, sheets, primary content surfaces.
    static let surface         = adaptive(light: 0xFFFFFF, dark: 0x1C1C1E)
    /// Subtle alternative surface — search bar fills, pressed rows.
    static let surfaceAlt      = adaptive(light: 0xF2F2F7, dark: 0x2C2C2E)
    /// Elevated surface — modals, popovers.
    static let surfaceElevated = adaptive(light: 0xFFFFFF, dark: 0x2C2C2E)

    // MARK: – Text
    static let textPrimary     = adaptive(light: 0x0B0B0F, dark: 0xF5F5F7)
    static let textSecondary   = adaptive(light: 0x4A4A55, dark: 0xB6B6BD)
    static let textTertiary    = adaptive(light: 0x8A8A93, dark: 0x787880)
    /// Always white — for use on top of solid color backgrounds (primary,
    /// success, danger).
    static let textInverse     = Color.white

    // MARK: – Borders
    static let border          = adaptive(light: 0xE5E5EA, dark: 0x2C2C2E)
    static let borderStrong    = adaptive(light: 0xC6C6CC, dark: 0x3A3A3C)
    static let divider         = adaptive(light: 0xEFEFF2, dark: 0x252528)

    // MARK: – Status
    static let success         = adaptive(light: 0x1F9D55, dark: 0x34C759)
    static let warning         = adaptive(light: 0xE08600, dark: 0xFFB340)
    static let danger          = adaptive(light: 0xD92D20, dark: 0xFF453A)
    static let info            = primary

    // MARK: – Avatar palette
    /// Short list of harmonious tints used as deterministic avatar
    /// backgrounds. Keep it short — recognizability beats variety.
    static let avatarPalette: [Color] = [
        Color(hex: 0x0A6CFF),
        Color(hex: 0x7C5CFF),
        Color(hex: 0xFF7A45),
        Color(hex: 0x1F9D55),
        Color(hex: 0xE6418E),
        Color(hex: 0x00A6A6),
    ]

    // MARK: – Helpers
    /// Resolves to a different shade per `userInterfaceStyle`. The
    /// `UIColor` initializer is re-evaluated whenever the user toggles
    /// dark mode at runtime, so views don't need to read `.colorScheme`.
    private static func adaptive(light: UInt32, dark: UInt32) -> Color {
        Color(UIColor { trait in
            UIColor(hex: trait.userInterfaceStyle == .dark ? dark : light)
        })
    }
}

// MARK: – Hex initializers

extension Color {
    init(hex: UInt32, alpha: Double = 1.0) {
        self.init(
            .sRGB,
            red:   Double((hex >> 16) & 0xFF) / 255.0,
            green: Double((hex >>  8) & 0xFF) / 255.0,
            blue:  Double( hex        & 0xFF) / 255.0,
            opacity: alpha
        )
    }
}

extension UIColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1.0) {
        self.init(
            red:   CGFloat((hex >> 16) & 0xFF) / 255.0,
            green: CGFloat((hex >>  8) & 0xFF) / 255.0,
            blue:  CGFloat( hex        & 0xFF) / 255.0,
            alpha: alpha
        )
    }
}
