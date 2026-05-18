import SwiftUI

/// A neutral padded container with the standard Helen card chrome.
/// Implementation defers entirely to `helenCardSurface()` — the modifier
/// owns the chrome, this view owns the padding contract.
struct HelenCard<Content: View>: View {
    var padding: CGFloat = HelenSpace.lg
    var elevation: HelenElevation = .none
    var background: Color? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .helenCardSurface(elevation: elevation, fill: background)
    }
}
