import SwiftUI

/// The "card" treatment — surface fill, hairline border, rounded corners —
/// was being repeated in five places before this. Apply once via modifier:
///
/// ```swift
/// VStack { … }.helenCardSurface()
/// ```
///
/// The token defaults match `HelenCard`; pass a different radius for
/// different rhythms (e.g. a pill, a tighter chip).
struct HelenCardSurface: ViewModifier {
    var cornerRadius: CGFloat = HelenRadius.lg
    var elevation: HelenElevation = .none
    var fill: Color? = nil

    @Environment(\.theme) private var theme

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        content
            .background(fill ?? theme.colors.surface)
            .overlay(shape.strokeBorder(theme.colors.border, lineWidth: 0.5))
            .clipShape(shape)
            .helenShadow(elevation)
    }
}

extension View {
    /// Wraps the receiver in the standard Helen card chrome (surface
    /// fill + hairline border + rounded corners + optional elevation).
    func helenCardSurface(
        cornerRadius: CGFloat = HelenRadius.lg,
        elevation: HelenElevation = .none,
        fill: Color? = nil
    ) -> some View {
        modifier(HelenCardSurface(
            cornerRadius: cornerRadius,
            elevation: elevation,
            fill: fill
        ))
    }
}
