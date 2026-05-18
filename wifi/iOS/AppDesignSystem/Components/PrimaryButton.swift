import SwiftUI

/// The single button component for the app.
///
/// Don't add new variants — exhaust these flags first. If a new style is
/// truly needed, add a case to `Variant` (rarely justified).
struct PrimaryButton: View {

    enum Variant {
        case primary       // filled accent — main CTA
        case secondary     // tinted accent on muted bg
        case ghost         // text-only
        case destructive   // filled danger — irreversible
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

    @Environment(\.isEnabled) private var envEnabled

    var body: some View {
        Button(action: invoke) {
            HStack(spacing: AppSpacing.sm) {
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
            .padding(.horizontal, AppSpacing.lg)
            .foregroundStyle(foreground)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: AppRadius.md, style: .continuous))
            .opacity(effectiveDisabled ? 0.45 : 1)
            .contentShape(RoundedRectangle(cornerRadius: AppRadius.md))
        }
        .disabled(effectiveDisabled || isLoading)
        .buttonStyle(PressableScaleStyle(
            scale: 0.985,
            brightness: pressedBrightness,
            hapticOnPress: false   // we fire a custom-tuned haptic in `invoke`
        ))
        .accessibilityLabel(Text(title))
        .accessibilityValue(isLoading ? Text("Loading") : Text(""))
        .accessibilityAddTraits(.isButton)
    }

    // MARK: – Actions
    private func invoke() {
        guard !isLoading else { return }
        UIImpactFeedbackGenerator(
            style: variant == .destructive ? .heavy : .light
        ).impactOccurred()
        action()
    }

    // MARK: – Styling
    private var effectiveDisabled: Bool { isDisabled || !envEnabled }

    private var heightForSize: CGFloat {
        size == .regular ? AppSize.buttonHeight : 40
    }
    private var textFont: Font {
        size == .regular ? AppTypography.bodyEmph
                         : AppTypography.subhead.weight(.semibold)
    }
    private var foreground: Color {
        switch variant {
        case .primary, .destructive: return AppColors.textInverse
        case .secondary, .ghost:     return AppColors.primary
        }
    }
    private var background: Color {
        switch variant {
        case .primary:     return AppColors.primary
        case .destructive: return AppColors.danger
        case .secondary:   return AppColors.primaryMuted
        case .ghost:       return .clear
        }
    }
    private var pressedBrightness: Double {
        switch variant {
        case .primary, .destructive: return -0.04
        case .secondary:             return -0.02
        case .ghost:                 return  0
        }
    }
}

#Preview {
    VStack(spacing: AppSpacing.md) {
        PrimaryButton(title: "Continue") {}
        PrimaryButton(title: "Add contact",   icon: "person.badge.plus",
                      variant: .secondary)   {}
        PrimaryButton(title: "Cancel",        variant: .ghost) {}
        PrimaryButton(title: "Delete account", icon: "trash",
                      variant: .destructive) {}
        PrimaryButton(title: "Loading",       isLoading: true) {}
        PrimaryButton(title: "Disabled",      isDisabled: true) {}
    }
    .padding()
    .background(AppColors.background)
}
