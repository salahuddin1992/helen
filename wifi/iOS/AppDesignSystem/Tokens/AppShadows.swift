import SwiftUI

/// Elevation tokens.
///
/// A shadow's job is to imply lift, not to look like a shadow. Keep them
/// short, soft, and adaptive — long blurry shadows look cheap on iOS.
struct AppShadow {
    let color:  Color
    let radius: CGFloat
    let x:      CGFloat
    let y:      CGFloat

    static let none = AppShadow(color: .clear, radius: 0, x: 0, y: 0)

    static let sm = AppShadow(
        color: adaptive(light: 0.06, dark: 0.40), radius: 4,  x: 0, y: 2
    )
    static let md = AppShadow(
        color: adaptive(light: 0.08, dark: 0.50), radius: 14, x: 0, y: 6
    )
    static let lg = AppShadow(
        color: adaptive(light: 0.12, dark: 0.60), radius: 28, x: 0, y: 10
    )

    /// Black with a different alpha per `userInterfaceStyle` — re-evaluated
    /// when the user toggles dark mode mid-session.
    private static func adaptive(light: CGFloat, dark: CGFloat) -> Color {
        Color(UIColor { trait in
            UIColor.black.withAlphaComponent(
                trait.userInterfaceStyle == .dark ? dark : light
            )
        })
    }
}

extension View {
    /// Apply a token shadow.
    func appShadow(_ shadow: AppShadow) -> some View {
        self.shadow(color: shadow.color,
                    radius: shadow.radius,
                    x: shadow.x, y: shadow.y)
    }
}
