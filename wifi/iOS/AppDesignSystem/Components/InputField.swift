import SwiftUI

/// A text input with optional label, leading symbol, helper text, and
/// error state. The single input pattern for the app.
struct InputField: View {

    let label: LocalizedStringKey
    @Binding var text: String
    var placeholder: LocalizedStringKey? = nil
    var icon: String? = nil
    var keyboard: UIKeyboardType = .default
    var contentType: UITextContentType? = nil
    var isSecure: Bool = false
    var helper: LocalizedStringKey? = nil
    var error: LocalizedStringKey? = nil
    var submitLabel: SubmitLabel = .next
    var onSubmit: (() -> Void)? = nil

    @FocusState private var focused: Bool
    @State private var revealed = false

    var body: some View {
        VStack(alignment: .leading, spacing: AppSpacing.xs) {
            Text(label)
                .font(AppTypography.caption.weight(.medium))
                .foregroundStyle(AppColors.textSecondary)
                .padding(.leading, AppSpacing.xs)

            HStack(spacing: AppSpacing.sm) {
                if let icon {
                    Image(systemName: icon)
                        .font(.body)
                        .foregroundStyle(AppColors.textTertiary)
                        .accessibilityHidden(true)
                }

                Group {
                    if isSecure && !revealed {
                        SecureField(text: $text) {
                            placeholder.map { Text($0).foregroundStyle(AppColors.textTertiary) }
                        }
                    } else {
                        TextField(text: $text) {
                            placeholder.map { Text($0).foregroundStyle(AppColors.textTertiary) }
                        }
                    }
                }
                .focused($focused)
                .keyboardType(keyboard)
                .textContentType(contentType)
                .autocorrectionDisabled(isSecure)
                .textInputAutocapitalization(isSecure ? .never : .sentences)
                .submitLabel(submitLabel)
                .onSubmit { onSubmit?() }
                .font(AppTypography.body)
                .foregroundStyle(AppColors.textPrimary)
                .tint(AppColors.primary)

                if isSecure {
                    Button { revealed.toggle() } label: {
                        Image(systemName: revealed ? "eye.slash" : "eye")
                            .foregroundStyle(AppColors.textTertiary)
                    }
                    .accessibilityLabel(revealed ? "Hide" : "Show")
                }
            }
            .frame(height: AppSize.inputHeight)
            .padding(.horizontal, AppSpacing.md)
            .background(AppColors.surface)
            .overlay(
                RoundedRectangle(cornerRadius: AppRadius.md, style: .continuous)
                    .strokeBorder(borderColor, lineWidth: focused ? 1.5 : 1)
                    .animation(AppMotion.quick, value: focused)
                    .animation(AppMotion.quick, value: error == nil)
            )
            .clipShape(RoundedRectangle(cornerRadius: AppRadius.md, style: .continuous))

            if let error {
                Label { Text(error) } icon: {
                    Image(systemName: "exclamationmark.circle.fill")
                }
                .font(AppTypography.footnote)
                .foregroundStyle(AppColors.danger)
                .padding(.leading, AppSpacing.xs)
                .transition(.opacity.combined(with: .move(edge: .top)))
            } else if let helper {
                Text(helper)
                    .font(AppTypography.footnote)
                    .foregroundStyle(AppColors.textTertiary)
                    .padding(.leading, AppSpacing.xs)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var borderColor: Color {
        if error != nil  { return AppColors.danger }
        if focused       { return AppColors.primary }
        return AppColors.border
    }
}
