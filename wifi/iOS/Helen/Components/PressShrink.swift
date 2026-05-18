import SwiftUI

/// Press feedback — gentle scale, fast easing. Apply to any custom-styled
/// button. Don't use on `Button(role:)` rows inside a `List`; the system
/// already handles those.
struct PressShrink: ButtonStyle {
    var scale: CGFloat = 0.96

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .animation(Theme.Motion.press, value: configuration.isPressed)
    }
}
