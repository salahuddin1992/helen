import SwiftUI
import UIKit

/// Design tokens.
///
/// Color and font don't appear here — they come from the system (`.primary`,
/// `.secondary`, `Color.accentColor`, system text styles). That's
/// deliberate: it's how we get free Dark Mode, Dynamic Type, and tint
/// customization without a per-component branch.
enum Theme {

    /// 4-pt spacing scale.
    enum Space {
        static let xs:  CGFloat = 4
        static let sm:  CGFloat = 8
        static let md:  CGFloat = 12
        static let lg:  CGFloat = 16
        static let xl:  CGFloat = 20
        static let xxl: CGFloat = 24
    }

    /// Corner radius scale. Match Apple's hierarchy.
    enum Radius {
        static let sm: CGFloat = 10
        static let md: CGFloat = 14
        static let lg: CGFloat = 18
    }

    /// Animation timings. Keep motion functional, not decorative.
    enum Motion {
        static let snappy = Animation.snappy(duration: 0.22)
        static let spring = Animation.spring(response: 0.32, dampingFraction: 0.86)
        static let press  = Animation.easeOut(duration: 0.14)
    }
}

/// Centralised haptic feedback. One channel — one place to change.
enum Haptic {
    /// A light selection tick (toggle change, copy succeeded).
    static func selection() {
        UISelectionFeedbackGenerator().selectionChanged()
    }
    /// A neutral impact (call action button pressed).
    static func tap(_ style: UIImpactFeedbackGenerator.FeedbackStyle = .light) {
        UIImpactFeedbackGenerator(style: style).impactOccurred()
    }
    /// A notification haptic (call ended, action submitted).
    static func notice(_ type: UINotificationFeedbackGenerator.FeedbackType) {
        UINotificationFeedbackGenerator().notificationOccurred(type)
    }
}
