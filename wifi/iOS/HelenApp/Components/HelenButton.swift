import SwiftUI

/// One opinionated button covering every variant we ship.
/// Resist adding new variants — exhaust these flags first.
struct HelenButton: View {

    enum Variant {
        case primary       // filled accent — main CTA
        case secondary     // tinted accent on muted bg — secondary CTA
        case ghost         // text-only — destructive secondary or low-emphasis
        case destructive   // filled danger — irreversible action
    }

    enum Size {
        case regular, compact
    }

    let title: LocalizedStringKey
    var icon: String? = nil
    var variant: Variant = .primary
    var size: Size = .regular
    var fullWidth: Bool = true
    var isLoading: Bool = false
    var isDisabled: Bool = false
    let action: () -> Void

    @Environment(\.theme)     private var theme
    @Environment(\.isEnabled) private var envEnabled

    var body: some View {
        Button(action: invoke) {
            HStack(spacing: HelenSpace.sm) {
                if isLoading {
                    ProgressView()
                        .controlSize(.small)
                        .tint(foreground)
                        .accessibilityHidden(true)
                } else if let icon {
                    Image(systemName: icon)
                        .font(.body.weight(.semibold))
                        .accessibilityHidden(true)
                }
                Text(title)
                    .font(textFont)
                    .opacity(isLoading ? 0.6 : 1)
            }
            .frame(maxWidth: fullWidth ? .infinity : nil)
            .frame(height: heightForSize)
            .padding(.horizontal, HelenSpace.lg)
            .foregroundStyle(foreground)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous))
            .opacity(effectiveDisabled ? 0.45 : 1)
            .contentShape(RoundedRectangle(cornerRadius: HelenRadius.md))
        }
        .disabled(effectiveDisabled || isLoading)
        .buttonStyle(HelenButtonPressStyle(variant: variant))
        .accessibilityLabel(Text(title))
        .accessibilityValue(isLoading ? Text("Loading") : Text(""))
        .accessibilityAddTraits(.isButton)
        .accessibilityRemoveTraits(effectiveDisabled ? .isButton : [])
    }

    // MARK: – derived

    private var effectiveDisabled: Bool { isDisabled || !envEnabled }
    private var heightForSize: CGFloat {
        size == .regular ? HelenSize.buttonHeight : 40
    }
    private var textFont: Font {
        size == .regular ? HelenFont.bodyEmph
                         : HelenFont.subhead.weight(.semibold)
    }
    private var foreground: Color {
        switch variant {
        case .primary, .destructive: return theme.colors.textOnAccent
        case .secondary, .ghost:     return theme.colors.accent
        }
    }
    private var background: Color {
        switch variant {
        case .primary:     return theme.colors.accent
        case .destructive: return theme.colors.danger
        case .secondary:   return theme.colors.accentMuted
        case .ghost:       return .clear
        }
    }

    private func invoke() {
        guard !isLoading else { return }
        UIImpactFeedbackGenerator(
            style: variant == .destructive ? .heavy : .light
        ).impactOccurred()
        action()
    }
}

/// Press state: subtle dim + scale, tuned per variant so the filled
/// buttons read more "pushed" than the text-only ghost.
private struct HelenButtonPressStyle: ButtonStyle {
    let variant: HelenButton.Variant
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .brightness(configuration.isPressed ? pressedBrightness : 0)
            .animation(HelenMotion.quick, value: configuration.isPressed)
    }
    private var pressedBrightness: Double {
        switch variant {
        case .primary, .destructive: return -0.04
        case .secondary:             return -0.02
        case .ghost:                 return  0
        }
    }
}

#Preview("Buttons") {
    VStack(spacing: HelenSpace.md) {
        HelenButton(title: "Continue",       variant: .primary)     {}
        HelenButton(title: "Add contact",    icon: "person.badge.plus",
                    variant: .secondary)                            {}
        HelenButton(title: "Cancel",         variant: .ghost)       {}
        HelenButton(title: "Delete account", icon: "trash",
                    variant: .destructive)                          {}
        HelenButton(title: "Loading",        variant: .primary,
                    isLoading: true)                                {}
        HelenButton(title: "Disabled",       variant: .primary,
                    isDisabled: true)                               {}
    }
    .padding()
    .background(HelenColor.background)
}
