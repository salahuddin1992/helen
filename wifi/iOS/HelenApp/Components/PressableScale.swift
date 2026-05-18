import SwiftUI

/// A press-feedback modifier — scales down slightly while a finger is on the
/// view and triggers a soft haptic. Use on any custom interactive surface.
struct PressableScaleStyle: ButtonStyle {
    var scale: CGFloat = 0.97
    var hapticOnPress: Bool = true

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .animation(HelenMotion.quick, value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, pressed in
                guard hapticOnPress, pressed else { return }
                UIImpactFeedbackGenerator(style: .soft).impactOccurred(intensity: 0.6)
            }
    }
}

extension View {
    /// Wrap any tap-able view in a Button that uses the press-scale style.
    func pressableScale(action: @escaping () -> Void) -> some View {
        Button(action: action) { self }
            .buttonStyle(PressableScaleStyle())
    }
}
