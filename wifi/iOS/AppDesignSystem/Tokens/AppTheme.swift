import SwiftUI

/// Top-level entry point for the design system.
///
/// Two valid access paths:
/// 1. **Direct** — `AppColors.primary`, `AppTypography.body`, `AppSpacing.lg`.
///    This is the canonical path. Use it everywhere.
/// 2. **Environment** — `@Environment(\.appTheme)`. Use this only when you
///    need to swap themes at runtime (e.g. a high-contrast variant). The
///    environment instance proxies to the same tokens.
///
/// Don't introduce a third path. Two is enough.
struct AppTheme: Equatable {
    var colors = AppColorPalette()
    var motion = AppMotionPalette()

    /// The default palette, matching the canonical token enums.
    static let `default` = AppTheme()
}

/// A struct mirror of `AppColors` so themes can swap the whole palette
/// without touching call sites.
struct AppColorPalette: Equatable {
    var primary         = AppColors.primary
    var primaryMuted    = AppColors.primaryMuted
    var primaryPressed  = AppColors.primaryPressed
    var background      = AppColors.background
    var surface         = AppColors.surface
    var surfaceAlt      = AppColors.surfaceAlt
    var surfaceElevated = AppColors.surfaceElevated
    var textPrimary     = AppColors.textPrimary
    var textSecondary   = AppColors.textSecondary
    var textTertiary    = AppColors.textTertiary
    var textInverse     = AppColors.textInverse
    var border          = AppColors.border
    var borderStrong    = AppColors.borderStrong
    var divider         = AppColors.divider
    var success         = AppColors.success
    var warning         = AppColors.warning
    var danger          = AppColors.danger
}

struct AppMotionPalette: Equatable {
    var quick    = AppMotion.quick
    var standard = AppMotion.standard
    var gentle   = AppMotion.gentle
}

// MARK: – Environment

private struct AppThemeKey: EnvironmentKey {
    static let defaultValue: AppTheme = .default
}

extension EnvironmentValues {
    var appTheme: AppTheme {
        get { self[AppThemeKey.self] }
        set { self[AppThemeKey.self] = newValue }
    }
}
