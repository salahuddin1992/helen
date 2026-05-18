import SwiftUI

/// Caller avatar with a soft "breathing" ring while the call is active.
/// Two concentric pulses fade out as they expand, mimicking the visual
/// signature of FaceTime / Phone during a connected call.
struct CallerAvatarView: View {
    let name: String
    var imageURL: URL? = nil
    var size: CGFloat = 168

    @State private var pulse: Bool = false

    var body: some View {
        ZStack {
            // Outer pulses — two staggered rings.
            ForEach(0..<2, id: \.self) { i in
                Circle()
                    .stroke(Color.white.opacity(pulse ? 0 : 0.35), lineWidth: 2)
                    .scaleEffect(pulse ? 1.45 : 1.0)
                    .frame(width: size, height: size)
                    .animation(
                        .easeOut(duration: 2.2)
                        .repeatForever(autoreverses: false)
                        .delay(Double(i) * 1.1),
                        value: pulse
                    )
            }

            // Soft halo glow.
            Circle()
                .fill(Color.white.opacity(0.12))
                .frame(width: size + 24, height: size + 24)
                .blur(radius: 16)

            // Avatar disc — bordered for clean separation against the gradient.
            HelenAvatar(name: name, imageURL: imageURL, size: .xl)
                .scaleEffect(size / HelenSize.avatarXl)
                .overlay(
                    Circle().strokeBorder(.white.opacity(0.35), lineWidth: 1)
                        .frame(width: size, height: size)
                )
                .scaleEffect(pulse ? 1.02 : 1.0)
                .animation(
                    .easeInOut(duration: 1.6).repeatForever(autoreverses: true),
                    value: pulse
                )
        }
        .frame(width: size + 32, height: size + 32)
        .onAppear { pulse = true }
        .accessibilityHidden(true)
    }
}
