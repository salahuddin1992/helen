import SwiftUI

/// A text field with a floating label, helper/error text, and an optional
/// leading SF Symbol. Designed to be the only input style in the app.
struct HelenTextField: View {

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
    @Environment(\.theme) private var theme
    @State private var revealed = false

    var body: some View {
        VStack(alignment: .leading, spacing: HelenSpace.xs) {
            Text(label)
                .font(HelenFont.caption.weight(.medium))
                .foregroundStyle(theme.colors.textSecondary)
                .padding(.leading, HelenSpace.xs)

            HStack(spacing: HelenSpace.sm) {
                if let icon {
                    Image(systemName: icon)
                        .font(.body)
                        .foregroundStyle(theme.colors.textTertiary)
                        .accessibilityHidden(true)
                }

                Group {
                    if isSecure && !revealed {
                        SecureField(text: $text) {
                            if let placeholder {
                                Text(placeholder).foregroundStyle(theme.colors.textTertiary)
                            }
                        }
                    } else {
                        TextField(text: $text) {
                            if let placeholder {
                                Text(placeholder).foregroundStyle(theme.colors.textTertiary)
                            }
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
                .font(HelenFont.body)
                .foregroundStyle(theme.colors.textPrimary)
                .tint(theme.colors.accent)

                if isSecure {
                    Button { revealed.toggle() } label: {
                        Image(systemName: revealed ? "eye.slash" : "eye")
                            .foregroundStyle(theme.colors.textTertiary)
                    }
                    .accessibilityLabel(revealed ? "Hide password" : "Show password")
                }
            }
            .frame(height: HelenSize.inputHeight)
            .padding(.horizontal, HelenSpace.md)
            .background(theme.colors.surface)
            .overlay(
                RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous)
                    .strokeBorder(borderColor, lineWidth: focused ? 1.5 : 1)
                    .animation(HelenMotion.quick, value: focused)
                    .animation(HelenMotion.quick, value: error == nil)
            )
            .clipShape(RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous))

            if let error {
                Label { Text(error) } icon: { Image(systemName: "exclamationmark.circle.fill") }
                    .font(HelenFont.footnote)
                    .foregroundStyle(theme.colors.danger)
                    .padding(.leading, HelenSpace.xs)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            } else if let helper {
                Text(helper)
                    .font(HelenFont.footnote)
                    .foregroundStyle(theme.colors.textTertiary)
                    .padding(.leading, HelenSpace.xs)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var borderColor: Color {
        if error != nil  { return theme.colors.danger }
        if focused       { return theme.colors.accent }
        return theme.colors.border
    }
}

#Preview {
    @Previewable @State var name = ""
    @Previewable @State var pw = ""
    return VStack(spacing: HelenSpace.lg) {
        HelenTextField(label: "Username", text: $name, placeholder: "yourname",
                       icon: "person", helper: "Letters and numbers only")
        HelenTextField(label: "Password", text: $pw, placeholder: "••••••••",
                       icon: "lock", isSecure: true,
                       error: pw.count < 6 && !pw.isEmpty ? "At least 6 characters" : nil)
    }
    .padding()
    .background(HelenColor.background)
}
