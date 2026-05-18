import SwiftUI

/// Near-black wash with a single soft tint behind the caller's name.
/// One color, one stop — restraint is the design.
struct CallBackdrop: View {
    var body: some View {
        ZStack {
            Color.black
            RadialGradient(
                colors: [Color(red: 0.20, green: 0.22, blue: 0.32).opacity(0.8), .clear],
                center: UnitPoint(x: 0.5, y: 0.18),
                startRadius: 60,
                endRadius: 540
            )
        }
        .ignoresSafeArea()
    }
}
