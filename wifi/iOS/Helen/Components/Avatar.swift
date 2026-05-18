import SwiftUI

/// Monochrome avatar — neutral fill with secondary initials.
///
/// Color is not part of this component's visual language. Differentiation
/// comes from the initials and surrounding context. The `onDark` palette
/// is the one exception (white-on-translucent for legibility on the call
/// screen's near-black backdrop).
struct Avatar: View {

    enum Palette { case standard, onDark }

    let name: String
    var diameter: CGFloat = 36
    var palette: Palette = .standard

    var body: some View {
        ZStack {
            base
            Text(initials)
                .font(.system(size: diameter * 0.4,
                              weight: .semibold,
                              design: .rounded))
                .foregroundStyle(foreground)
        }
        .frame(width: diameter, height: diameter)
        .clipShape(Circle())
        .accessibilityLabel("\(name) avatar")
    }

    @ViewBuilder
    private var base: some View {
        switch palette {
        case .standard: Circle().fill(.fill.secondary)
        case .onDark:   Circle().fill(.white.opacity(0.18))
        }
    }

    private var foreground: Color {
        switch palette {
        case .standard: return .secondary
        case .onDark:   return .white
        }
    }

    private var initials: String {
        let parts = name
            .split(whereSeparator: { $0.isWhitespace })
            .prefix(2)
        return parts
            .compactMap { $0.first }
            .map { String($0).uppercased() }
            .joined()
    }
}
