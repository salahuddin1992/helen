import SwiftUI

/// Elevation tokens.
///
/// Shadows on iOS look cheap when they're long and blurry — keep them
/// short, soft, and adaptive. In dark mode, a black shadow disappears
/// against a near-black background; we lift it slightly with extra alpha.
struct HelenElevation {
    let color:  Color
    let radius: CGFloat
    let x:      CGFloat
    let y:      CGFloat

    static let none = HelenElevation(color: .clear, radius: 0, x: 0, y: 0)

    static let sm   = HelenElevation(
        color: adaptive(light: 0.06, dark: 0.40), radius: 4,  x: 0, y: 2
    )
    static let md   = HelenElevation(
        color: adaptive(light: 0.08, dark: 0.50), radius: 14, x: 0, y: 6
    )
    static let lg   = HelenElevation(
        color: adaptive(light: 0.12, dark: 0.60), radius: 28, x: 0, y: 10
    )

    /// Black with a different alpha per appearance — `Color(UIColor { … })`
    /// re-resolves automatically when the user toggles dark mode at runtime.
    private static func adaptive(light: CGFloat, dark: CGFloat) -> Color {
        Color(UIColor { trait in
            UIColor.black.withAlphaComponent(
                trait.userInterfaceStyle == .dark ? dark : light
            )
        })
    }
}

extension View {
    func helenShadow(_ elevation: HelenElevation) -> some View {
        self.shadow(
            color:  elevation.color,
            radius: elevation.radius,
            x:      elevation.x,
            y:      elevation.y
        )
    }
}
