import SwiftUI

/// Helen type system.
///
/// Built on top of the iOS text styles so Dynamic Type works automatically.
/// Use these tokens — never hard-code `.system(size:)` in feature code.
enum HelenFont {

    // Display — only for hero moments (sign-in title, empty states).
    static let display    = Font.system(.largeTitle, design: .rounded).weight(.bold)

    // Page-level titles.
    static let title      = Font.system(.title, design: .rounded).weight(.semibold)
    static let title2     = Font.system(.title2, design: .rounded).weight(.semibold)
    static let title3     = Font.system(.title3, design: .rounded).weight(.semibold)

    // Section/card headings.
    static let headline   = Font.system(.headline).weight(.semibold)
    static let subhead    = Font.system(.subheadline).weight(.medium)

    // Body copy.
    static let body       = Font.system(.body)
    static let bodyMed    = Font.system(.body).weight(.medium)
    static let bodyEmph   = Font.system(.body).weight(.semibold)

    // Supporting text.
    static let callout    = Font.system(.callout)
    static let footnote   = Font.system(.footnote)
    static let caption    = Font.system(.caption)
    static let caption2   = Font.system(.caption2)

    // Numerics — tabular figures keep alignment in lists & timestamps.
    static let monoBody   = Font.system(.body, design: .monospaced)
    static let tabular    = Font.system(.subheadline).monospacedDigit()
}
