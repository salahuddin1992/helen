import SwiftUI

/// Type scale — all derived from `Font.system(_, design:)` so Dynamic Type
/// scales every token automatically. Never hard-code `.system(size: 17)`
/// in feature code; pick the closest semantic token here.
enum AppTypography {

    // MARK: – Display & titles (rounded for premium feel)
    static let display    = Font.system(.largeTitle, design: .rounded).weight(.bold)
    static let title      = Font.system(.title,      design: .rounded).weight(.semibold)
    static let title2     = Font.system(.title2,     design: .rounded).weight(.semibold)
    static let title3     = Font.system(.title3,     design: .rounded).weight(.semibold)

    // MARK: – Section / card heads
    static let headline   = Font.system(.headline).weight(.semibold)
    static let subhead    = Font.system(.subheadline).weight(.medium)

    // MARK: – Body
    static let body       = Font.system(.body)
    static let bodyMed    = Font.system(.body).weight(.medium)
    static let bodyEmph   = Font.system(.body).weight(.semibold)

    // MARK: – Supporting
    static let callout    = Font.system(.callout)
    static let footnote   = Font.system(.footnote)
    static let caption    = Font.system(.caption)
    static let caption2   = Font.system(.caption2)

    // MARK: – Numerics
    /// For prices, durations, phone numbers — keeps columns aligned.
    static let tabular    = Font.system(.subheadline).monospacedDigit()
    static let mono       = Font.system(.body, design: .monospaced)
}
