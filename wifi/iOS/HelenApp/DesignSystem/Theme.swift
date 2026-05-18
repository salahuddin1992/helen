import SwiftUI

/// A lightweight environment that lets feature code reach for tokens
/// through `@Environment(\.theme)` without importing every namespace.
struct HelenTheme {
    var colors  = HelenColors()
    var motion  = HelenMotion.self
    var radius  = HelenRadius.self
    var space   = HelenSpace.self
    var size    = HelenSize.self
}

/// A struct of color tokens (instead of an enum) so future themes can swap
/// the whole palette wholesale (e.g. high-contrast variant).
struct HelenColors {
    let accent          = HelenColor.accent
    let accentMuted     = HelenColor.accentMuted
    let accentPressed   = HelenColor.accentPressed
    let background      = HelenColor.background
    let surface         = HelenColor.surface
    let surfaceAlt      = HelenColor.surfaceAlt
    let surfaceElevated = HelenColor.surfaceElevated
    let textPrimary     = HelenColor.textPrimary
    let textSecondary   = HelenColor.textSecondary
    let textTertiary    = HelenColor.textTertiary
    let textOnAccent    = HelenColor.textOnAccent
    let border          = HelenColor.border
    let borderStrong    = HelenColor.borderStrong
    let divider         = HelenColor.divider
    let success         = HelenColor.success
    let warning         = HelenColor.warning
    let danger          = HelenColor.danger
}

private struct HelenThemeKey: EnvironmentKey {
    static let defaultValue = HelenTheme()
}

extension EnvironmentValues {
    var theme: HelenTheme {
        get { self[HelenThemeKey.self] }
        set { self[HelenThemeKey.self] = newValue }
    }
}
