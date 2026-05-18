import SwiftUI

/// A search field with iOS-style chrome — magnifier glass, clear button,
/// and an optional cancel button that animates in when focused.
struct HelenSearchBar: View {
    @Binding var text: String
    var placeholder: LocalizedStringKey = "Search"
    var showCancel: Bool = true

    @FocusState private var focused: Bool
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.sm) {
            HStack(spacing: HelenSpace.sm) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(theme.colors.textTertiary)
                    .accessibilityHidden(true)
                TextField(text: $text) {
                    Text(placeholder).foregroundStyle(theme.colors.textTertiary)
                }
                .focused($focused)
                .submitLabel(.search)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .foregroundStyle(theme.colors.textPrimary)
                .tint(theme.colors.accent)
                if !text.isEmpty {
                    Button { text = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(theme.colors.textTertiary)
                    }
                    .transition(.opacity)
                    .accessibilityLabel("Clear")
                }
            }
            .padding(.horizontal, HelenSpace.md)
            .frame(height: 40)
            .background(theme.colors.surfaceAlt)
            .clipShape(RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous))

            if showCancel && focused {
                Button { focused = false; text = "" } label: {
                    Text("Cancel")
                        .font(HelenFont.bodyMed)
                        .foregroundStyle(theme.colors.accent)
                }
                .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .animation(HelenMotion.standard, value: focused)
        .animation(HelenMotion.quick, value: text.isEmpty)
    }
}
