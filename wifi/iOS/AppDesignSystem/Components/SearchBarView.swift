import SwiftUI

/// Search field with iOS-style chrome — magnifier glass, clear button,
/// and an animated cancel button that appears when focused.
struct SearchBarView: View {
    @Binding var text: String
    var placeholder: LocalizedStringKey = "Search"
    var showCancel: Bool = true

    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: AppSpacing.sm) {
            HStack(spacing: AppSpacing.sm) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(AppColors.textTertiary)
                    .accessibilityHidden(true)

                TextField(text: $text) {
                    Text(placeholder).foregroundStyle(AppColors.textTertiary)
                }
                .focused($focused)
                .submitLabel(.search)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .foregroundStyle(AppColors.textPrimary)
                .tint(AppColors.primary)

                if !text.isEmpty {
                    Button { text = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(AppColors.textTertiary)
                    }
                    .accessibilityLabel("Clear")
                    .transition(.opacity)
                }
            }
            .padding(.horizontal, AppSpacing.md)
            .frame(height: 40)
            .background(AppColors.surfaceAlt)
            .clipShape(RoundedRectangle(cornerRadius: AppRadius.md, style: .continuous))

            if showCancel && focused {
                Button {
                    focused = false
                    text = ""
                } label: {
                    Text("Cancel")
                        .font(AppTypography.bodyMed)
                        .foregroundStyle(AppColors.primary)
                }
                .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .animation(AppMotion.standard, value: focused)
        .animation(AppMotion.quick, value: text.isEmpty)
    }
}
