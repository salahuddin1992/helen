import SwiftUI

/// Press-feedback button style — gentle scale + soft haptic. Apply to any
/// `Button` to get the system's standard tap feel without rewriting it.
struct PressableScaleStyle: ButtonStyle {
    var scale: CGFloat = 0.97
    var brightness: Double = 0
    var hapticOnPress: Bool = true

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .brightness(configuration.isPressed ? brightness : 0)
            .animation(AppMotion.quick, value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, pressed in
                guard hapticOnPress, pressed else { return }
                UIImpactFeedbackGenerator(style: .soft)
                    .impactOccurred(intensity: 0.6)
            }
    }
}
