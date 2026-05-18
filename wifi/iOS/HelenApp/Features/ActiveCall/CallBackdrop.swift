import SwiftUI

/// The luxe gradient that sits behind the entire active-call screen.
///
/// Two adaptive stops — deeper near the top so the caller info reads on a
/// rich color, fading to a near-black/near-white at the bottom so the
/// action grid floats. Uses `Color(UIColor { … })` so it tracks dark mode
/// without manual `colorScheme` reads.
struct CallBackdrop: View {
    var body: some View {
        LinearGradient(
            colors: [topColor, midColor, bottomColor],
            startPoint: .top,
            endPoint:   .bottom
        )
        .ignoresSafeArea()
        .overlay(noise.opacity(0.04))
    }

    private var topColor: Color {
        Color(UIColor { trait in
            trait.userInterfaceStyle == .dark
                ? UIColor(red: 0.10, green: 0.13, blue: 0.30, alpha: 1)
                : UIColor(red: 0.16, green: 0.36, blue: 0.86, alpha: 1)
        })
    }
    private var midColor: Color {
        Color(UIColor { trait in
            trait.userInterfaceStyle == .dark
                ? UIColor(red: 0.05, green: 0.06, blue: 0.16, alpha: 1)
                : UIColor(red: 0.32, green: 0.20, blue: 0.78, alpha: 1)
        })
    }
    private var bottomColor: Color {
        Color(UIColor { trait in
            trait.userInterfaceStyle == .dark
                ? UIColor(red: 0.02, green: 0.02, blue: 0.06, alpha: 1)
                : UIColor(red: 0.08, green: 0.08, blue: 0.20, alpha: 1)
        })
    }

    /// Subtle noise so the gradient never looks like a flat banded sky.
    private var noise: some View {
        Canvas { ctx, size in
            for _ in 0..<350 {
                let x = CGFloat.random(in: 0..<size.width)
                let y = CGFloat.random(in: 0..<size.height)
                let r = CGFloat.random(in: 0.4...1.4)
                ctx.fill(
                    Path(ellipseIn: CGRect(x: x, y: y, width: r, height: r)),
                    with: .color(.white.opacity(0.5))
                )
            }
        }
        .blendMode(.overlay)
        .allowsHitTesting(false)
    }
}
