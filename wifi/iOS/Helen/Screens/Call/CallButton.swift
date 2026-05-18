import SwiftUI

// MARK: - Toggle (mute, speaker, video)

struct CallToggle: View {
    let off: String
    let on:  String
    let title: LocalizedStringKey
    @Binding var isOn: Bool

    var body: some View {
        Button {
            Haptic.selection()
            withAnimation(Theme.Motion.snappy) { isOn.toggle() }
        } label: {
            Shell(symbol: isOn ? on : off, title: title, active: isOn)
        }
        .buttonStyle(PressShrink(scale: 0.94))
        .accessibilityLabel(title)
        .accessibilityValue(isOn ? "On" : "Off")
        .accessibilityAddTraits(isOn ? [.isButton, .isSelected] : .isButton)
    }
}

// MARK: - One-shot (keypad, add, contacts)

struct CallTap: View {
    let symbol: String
    let title: LocalizedStringKey
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.tap()
            action()
        } label: {
            Shell(symbol: symbol, title: title, active: false)
        }
        .buttonStyle(PressShrink(scale: 0.94))
        .accessibilityLabel(title)
    }
}

// MARK: - Shared visual

private struct Shell: View {
    let symbol: String
    let title: LocalizedStringKey
    let active: Bool

    var body: some View {
        VStack(spacing: 8) {
            ZStack {
                Circle().fill(active ? Color.white : Color.white.opacity(0.15))
                Image(systemName: symbol)
                    .font(.system(size: 24, weight: .semibold))
                    .foregroundStyle(active ? Color.black : .white)
                    .contentTransition(.symbolEffect(.replace))
            }
            .frame(width: 70, height: 70)

            Text(title)
                .font(.caption2)
                .foregroundStyle(.white.opacity(0.85))
        }
    }
}
