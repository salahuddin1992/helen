import SwiftUI

/// A pressable surface — wraps a view in our standard rounded card with
/// the press-scale style applied. Use when you need a tappable card-like
/// element that isn't a `Button` or `NavigationLink` (rare).
struct HelenSurface<Content: View>: View {
    var padding: CGFloat = HelenSpace.lg
    var elevation: HelenElevation = .none
    let action: () -> Void
    @ViewBuilder var content: () -> Content

    var body: some View {
        Button(action: action) {
            content()
                .padding(padding)
                .frame(maxWidth: .infinity, alignment: .leading)
                .helenCardSurface(elevation: elevation)
        }
        .buttonStyle(PressableScaleStyle(scale: 0.99))
    }
}
