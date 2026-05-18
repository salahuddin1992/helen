# Helen — iOS (SwiftUI)

Production-grade SwiftUI scaffold for the Helen contacts/messaging app.

## Highlights
- 100% SwiftUI, no UIKit-only views
- Full light + dark color tokens
- LTR (English) + RTL (Arabic) localized
- SF Symbols throughout — zero external icon assets
- Dynamic Type ready (every font is a system text style)
- VoiceOver labels on every interactive element
- Renders cleanly from iPhone SE → iPhone 15 Pro Max
- Pure design tokens — no hard-coded hex / fonts / spacing in feature code

## Layout

```
HelenApp/
├── HelenApp.swift                 — entry point, root switch, multi-device previews
├── DesignSystem/                  — tokens (colors, typography, spacing, radii, shadows)
├── Components/                    — reusable views (button, textfield, avatar, …)
├── Models/                        — Contact, Conversation, ChatMessage
├── Features/
│   ├── Onboarding/SignInView
│   ├── Contacts/{ContactsView, ContactDetailView}
│   ├── Chats/{ChatsListView, ChatView}
│   ├── Profile/ProfileView
│   └── Settings/SettingsView
├── Navigation/RootTabView.swift
└── Resources/{en,ar}.lproj/Localizable.strings
```

## How to drop into Xcode

1. Open Xcode → **File ▸ New ▸ Project ▸ iOS App** named `Helen`, interface
   **SwiftUI**, language **Swift**, minimum deployment **iOS 17**.
2. Replace the auto-generated `HelenApp.swift` and `ContentView.swift` with
   the files in this folder (drag in the entire `HelenApp/` directory).
3. In **Project ▸ Info ▸ Localizations**, add **Arabic (ar)** alongside English.
4. Run on the simulator. Toggle:
   - Appearance: `Cmd+Shift+A` for dark mode
   - Language/region: scheme → **Run ▸ Options ▸ App Language**
   - Device: scheme → simulator picker (test SE through 15 Pro Max)

## Design philosophy

- **Restrained palette.** One accent (system blue), one neutral ramp,
  three status colors. No mood-driven gradients, no rainbow tint.
- **Type-driven hierarchy.** Headings carry weight; everything else is
  body or smaller. The eye goes to content, not chrome.
- **Surfaces over shadows.** Cards use a 0.5-pt border in `border` token
  rather than diffuse drop shadows — that's how iOS 17 reads native.
- **One way to do each thing.** A single `HelenButton`, a single
  `HelenTextField`. Variants are flags, not new types.
- **Motion is feedback.** All animations are < 350ms with `.spring`
  easing. Tap feedback uses haptics, not bounce.
