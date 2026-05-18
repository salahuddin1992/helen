import SwiftUI

/// Standard card chrome — surface fill, hairline border, large rounded
/// corners, optional elevation. The single source of truth for the card
/// look across the app. Apply via either:
///
/// ```swift
/// AppCard { content }                  // explicit container with padding
/// SomeView().appCardSurface()          // chrome-only, no padding
/// ```
struct AppCard<Content: View>: View {
    var padding: CGFloat = AppSpacing.lg
    var cornerRadius: CGFloat = AppRadius.lg
    var elevation: AppShadow = .none
    var fill: Color? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .appCardSurface(cornerRadius: cornerRadius,
                            elevation: elevation,
                            fill: fill)
    }
}

/// Modifier version — chrome only, no padding contract. Use when wrapping
/// content that already manages its own padding (a `VStack` of rows, a
/// table, etc.).
struct AppCardSurface: ViewModifier {
    var cornerRadius: CGFloat = AppRadius.lg
    var elevation: AppShadow = .none
    var fill: Color? = nil

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        content
            .background(fill ?? AppColors.surface)
            .overlay(shape.strokeBorder(AppColors.border, lineWidth: 0.5))
            .clipShape(shape)
            .appShadow(elevation)
    }
}

extension View {
    /// Wrap the receiver in standard card chrome without adding padding.
    func appCardSurface(
        cornerRadius: CGFloat = AppRadius.lg,
        elevation: AppShadow = .none,
        fill: Color? = nil
    ) -> some View {
        modifier(AppCardSurface(
            cornerRadius: cornerRadius,
            elevation: elevation,
            fill: fill
        ))
    }
}
