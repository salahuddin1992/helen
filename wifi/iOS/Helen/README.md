# Helen — iOS Calling

Production-grade SwiftUI app. iOS 17+. Single-target, no dependencies.

## Where each file goes in Xcode

Drag this `Helen/` folder into Xcode (File ▸ Add Files to Project, or drop
into the project navigator with **Create groups** selected). The structure
below maps 1-to-1 to Xcode groups.

```
Helen/
├── App/
│   └── HelenApp.swift            ← @main scene
├── DesignSystem/
│   └── Theme.swift               ← Spacing · Radius · Motion · Haptic
├── Domain/
│   └── Models.swift              ← Person, Call, MockData
├── Components/
│   ├── Avatar.swift              ← monochrome circular avatar
│   ├── EmptyState.swift          ← icon + title + message
│   └── PressShrink.swift         ← ButtonStyle for tap feedback
├── Screens/
│   ├── Root.swift                ← TabView
│   ├── Contacts/
│   │   ├── ContactsScreen.swift
│   │   └── PersonRow.swift
│   ├── Recents/
│   │   ├── RecentsScreen.swift
│   │   └── CallRow.swift
│   ├── Favorites/
│   │   └── FavoritesScreen.swift
│   ├── Detail/
│   │   ├── DetailScreen.swift
│   │   ├── ActionTile.swift
│   │   └── DetailField.swift
│   ├── Call/
│   │   ├── CallScreen.swift
│   │   ├── CallBackdrop.swift
│   │   └── CallButton.swift
│   └── Settings/
│       └── SettingsScreen.swift
└── Resources/
    ├── en.lproj/Localizable.strings
    └── ar.lproj/Localizable.strings
```

In **Project ▸ Info ▸ Localizations**, add Arabic alongside English.
That's the only project-level setting required.

## Design rules followed throughout

1. One accent color, one destructive color (red). Everything else is
   `.primary` / `.secondary` / `.tertiary`.
2. Avatars are monochrome — color is reserved for actionable items.
3. Two type weights: regular and semibold. No bold body text.
4. List-native everywhere. Custom card chrome only when justified.
5. Haptics only on toggles, copy, and call termination.
6. Materials over borders+shadows.
7. Built on system primitives — RTL, Dark Mode, Dynamic Type all work
   without per-component branching.
