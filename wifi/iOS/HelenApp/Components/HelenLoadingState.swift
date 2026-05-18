import SwiftUI

/// Centered loading view — `ProgressView` plus an optional caption.
/// Use during full-screen loads. For inline placeholders prefer
/// `HelenSkeleton`.
struct HelenLoadingState: View {
    var message: LocalizedStringKey? = nil

    @Environment(\.theme) private var theme

    var body: some View {
        VStack(spacing: HelenSpace.md) {
            ProgressView()
                .controlSize(.large)
                .tint(theme.colors.accent)
            if let message {
                Text(message)
                    .font(HelenFont.subhead)
                    .foregroundStyle(theme.colors.textSecondary)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(.vertical, HelenSpace.xxxl)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(message ?? "Loading")
        .accessibilityAddTraits(.updatesFrequently)
    }
}
