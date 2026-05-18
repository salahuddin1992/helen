# AppDesignSystem

A standalone, drop-in design system for an iOS calling app. SwiftUI-only,
zero external dependencies. Light + dark, LTR + RTL, Dynamic Type and
VoiceOver-ready out of the box.

## Layout

```
AppDesignSystem/
├── Tokens/
│   ├── AppColors.swift          — semantic light + dark color tokens
│   ├── AppTypography.swift      — type scale (rounded titles, system body)
│   ├── AppSpacing.swift         — 4-pt spacing, radii, sizes, motion
│   ├── AppShadows.swift         — adaptive elevation (sm/md/lg)
│   └── AppTheme.swift           — environment integration / theme swap
├── Components/
│   ├── PressableScale.swift     — press-feedback button style
│   ├── AppCard.swift            — card container + .appCardSurface() modifier
│   ├── PrimaryButton.swift      — primary/secondary/ghost/destructive
│   ├── InputField.swift         — labeled text field with helper/error
│   ├── Avatar.swift             — initials + presence dot (deterministic color)
│   ├── StatusBadge.swift        — pill + UnreadDot
│   ├── ContactCard.swift        — contact list-item card
│   ├── SearchBarView.swift      — iOS-style search field
│   └── EmptyStateView.swift     — symbol + title + message + CTA
└── Showcase/
    └── DesignSystemShowcase.swift — live gallery of every token + component
```

## How to use

### Direct token access
```swift
Text("Helen")
    .font(AppTypography.title)
    .foregroundStyle(AppColors.textPrimary)
    .padding(AppSpacing.lg)
```

### Card chrome
```swift
VStack { rows }
    .appCardSurface()                       // chrome only
// or
AppCard(elevation: .sm) { content }         // chrome + padding
```

### Buttons
```swift
PrimaryButton(title: "Continue") {}
PrimaryButton(title: "Add", icon: "person.badge.plus", variant: .secondary) {}
PrimaryButton(title: "Delete", variant: .destructive) {}
```

### Theme swap (advanced)
```swift
ContentView()
    .environment(\.appTheme, .default)      // or a custom palette
```

## Design rules

- **One way to do each thing.** A single button, a single text field,
  a single card. New variants are flags, never new types.
- **Tokens are semantic, not visual.** `textPrimary`, not `darkGray`.
  That's what makes dark mode free.
- **Restrained palette.** One accent, one neutral ramp, three status
  colors. No mood gradients, no rainbow tint.
- **Surfaces beat shadows.** Cards lean on a 0.5-pt hairline border.
  Shadows are tokens, used sparingly.
- **Motion is feedback.** All animations < 350ms with spring easing.
  Tap feedback is haptic + brightness, not bounce.

## Quality bar

- 100% SwiftUI, no UIKit-only views (UIKit only used for `UIColor`-backed
  adaptive resolution and haptics)
- Every Image inside a labeled button is `accessibilityHidden`
- Every interactive element has an `accessibilityLabel`
- Every text style is built on a system text style → Dynamic Type works
- Every color is `Color(UIColor { trait in … })` → dark mode is automatic
- Every shadow alpha is brighter in dark mode → elevation stays visible

## Render

Open `Showcase/DesignSystemShowcase.swift` and run any of the three
previews (Light, Dark on iPhone SE, Arabic RTL).
