import SwiftUI

/// 4-point spacing scale. Pick the smallest token that works.
enum AppSpacing {
    static let xxs:  CGFloat = 2
    static let xs:   CGFloat = 4
    static let sm:   CGFloat = 8
    static let md:   CGFloat = 12
    static let lg:   CGFloat = 16
    static let xl:   CGFloat = 20
    static let xxl:  CGFloat = 24
    static let xxxl: CGFloat = 32
    static let huge: CGFloat = 48

    /// Standard horizontal page inset.
    static let pageH: CGFloat = 20
    /// Standard vertical breathing room between page sections.
    static let pageV: CGFloat = 24
}

/// Corner radii. Match Apple's hierarchy — small for chips, medium for
/// inputs, large for cards, pill for capsules.
enum AppRadius {
    static let xs:   CGFloat = 6
    static let sm:   CGFloat = 10
    static let md:   CGFloat = 14
    static let lg:   CGFloat = 18
    static let xl:   CGFloat = 24
    static let pill: CGFloat = 999
}

/// Hit-target sizes. Apple HIG: 44pt minimum.
enum AppSize {
    static let minTap: CGFloat       = 44
    static let avatarSm: CGFloat     = 32
    static let avatarMd: CGFloat     = 44
    static let avatarLg: CGFloat     = 56
    static let avatarXl: CGFloat     = 88
    static let buttonHeight: CGFloat = 52
    static let inputHeight: CGFloat  = 52
}

/// Animation timings. Keep motion functional, not decorative.
enum AppMotion {
    static let quick    = Animation.easeOut(duration: 0.18)
    static let standard = Animation.spring(response: 0.32, dampingFraction: 0.86)
    static let gentle   = Animation.spring(response: 0.45, dampingFraction: 0.9)
}
