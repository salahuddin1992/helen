import SwiftUI

/// Floating action button. Sized and shadowed to feel like an Apple Maps
/// or Mail compose button.
struct AddContactFAB: View {
    var action: () -> Void

    @Environment(\.theme) private var theme

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            action()
        } label: {
            ZStack {
                Circle()
                    .fill(theme.colors.accent)
                    .helenShadow(.lg)
                Image(systemName: "person.crop.circle.badge.plus")
                    .font(.title3.weight(.bold))
                    .foregroundStyle(.white)
            }
            .frame(width: 60, height: 60)
        }
        .buttonStyle(PressableScaleStyle(scale: 0.92))
        .accessibilityLabel("Add new contact")
    }
}
