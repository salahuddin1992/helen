# MLO Mobile — Native iOS Target Architecture

This folder is the **reference architecture** for a native iOS app
matching the spec (universal iOS 12–18 support, dual UIKit + SwiftUI
targets, MLOCore Swift package, all transports, all screens).

## Why no Swift source here

Swift code that isn't compiled and tested on a Mac is just guesses.
We don't ship guesses. This folder documents the structure a Mac
developer should follow when implementing the app; the `iOS/web-
simulator/` sibling folder is what you can actually run today.

## Folder tree (target)

    MLOMobile/
    ├── App/
    │   ├── MLOMobileApp.swift          SwiftUI @main (iOS 14+)
    │   ├── AppDelegate.swift           UIKit fallback (iOS 12+)
    │   └── SceneDelegate.swift         iOS 13+
    ├── Core/                           Shared SPM package
    │   ├── Package.swift
    │   └── Sources/
    │       ├── Transport/              Link protocols + impls
    │       ├── Discovery/              Bonjour, NWBrowser, registry
    │       ├── Channel/                Scheduler, broadcast, aggregate
    │       ├── Crypto/                 Identity, NoiseIK, PinnedTLS
    │       ├── Persistence/            SwiftData / CoreData / Keychain
    │       └── Models/                 ServerClass, Instance, Path, Link
    ├── UI-Modern/                      SwiftUI (iOS 15+)
    │   ├── Screens/                    Dashboard, ServerList, etc.
    │   ├── Components/                 Glass cards, gauges, charts
    │   ├── ViewModels/                 @Observable or ObservableObject
    │   └── Theming/
    ├── UI-Legacy/                      UIKit (iOS 12+)
    │   ├── ViewControllers/
    │   ├── Views/
    │   └── Storyboards/
    ├── Platform/
    │   ├── iPhone/                     compact layouts, home indicator
    │   ├── iPad/                       SplitView, StageManager, Pencil
    │   ├── Catalyst/                   Mac menubar
    │   └── visionOS/                   immersive scenes
    ├── Features/
    │   ├── FileTransfer/
    │   ├── LiveTelemetry/
    │   ├── Chat/
    │   ├── VPN/                        NetworkExtension
    │   └── Widgets/                    WidgetKit (iOS 14+)
    ├── Accessibility/                  VoiceOver, DynamicType, RTL
    ├── Extensions/                     Share, NotificationService, etc.
    ├── Tests/                          XCTest + XCUITest + snapshot
    ├── Scripts/                        build, archive, fastlane
    ├── Resources/
    ├── Config/                         .xcconfig + entitlements
    ├── project.yml                     XcodeGen config
    ├── Package.swift                   SPM primary
    └── Podfile                         (CocoaPods only if legacy)

## Compatibility matrix

| iOS | Build target | UI layer | Concurrency |
|-----|--------------|----------|-------------|
| 12.x | Legacy | UIKit only | GCD + closures |
| 13.x | Either | UIKit + SwiftUI | Combine + GCD |
| 14.x | Either | UIKit + SwiftUI | Combine + GCD |
| 15.x | Either | UIKit + SwiftUI | async/await + Combine |
| 16.x | Modern | SwiftUI primary | async/await |
| 17.x | Modern | SwiftUI + @Observable | async/await + actors |
| 18.x | Modern | SwiftUI + new APIs | async/await + actors |

Two Xcode build targets (`MLOMobile-Legacy` iOS 12.0+, `MLOMobile-
Modern` iOS 15.0+) share the same MLOCore Swift package. Conditional
compilation with `#available()` + `@available` attributes routes to
the right APIs.

## Transport matrix (MLOCore)

- WiFi LAN — Bonjour via NWBrowser / NSNetService
- WiFi Internet — URLSessionWebSocketTask / Starscream
- Cellular — NWParameters.requiredInterfaceType = .cellular
- MultipeerConnectivity — MCSession + Advertiser + Browser
- Core Bluetooth — Custom GATT service "_mlo._tcp" mapped over BLE
- Personal Hotspot — Bonjour over bridge100 interface
- USB-tethered — peertalk via usbmuxd (for Mac companion)

## Security posture

- Ed25519 device identity, Secure Enclave on iOS 14+, Keychain otherwise
- TLS 1.3 with SPKI pinning (pre-baked hashes)
- Face ID / Touch ID unlock
- NSFileProtectionComplete at rest
- No third-party SDKs; MetricKit + OSLog for diagnostics

## Build-system notes

```bash
# on macOS only:
brew install xcodegen
cd MLOMobile
xcodegen generate
open MLOMobile.xcodeproj

# fastlane lanes:
fastlane beta     # uploads to TestFlight
fastlane release  # uploads to App Store Connect
```

## Device test matrix (manual QA)

- iPhone SE (1st gen, iOS 12.5.7)
- iPhone 8 (iOS 16.x)
- iPhone 13 (iOS 17.x)
- iPhone 15 Pro (iOS 18.x)
- iPhone 16 Pro Max (iOS 18.x) — primary layout target
- iPad mini 6 (iOS 18.x)
- iPad Pro 13" M4 (iOS 18.x)
- Mac Catalyst
- visionOS simulator

## Web simulator vs native

For verifying UX flows WITHOUT a Mac, use `iOS/web-simulator/`. The
web simulator:
- Renders at iPhone 16 Pro Max dimensions (430×932 points)
- Uses CSS backdrop-filter for Liquid Glass look
- Exercises the same Helen REST + Socket.IO endpoints the native
  app would call
- Works in Chrome/Firefox/Safari on any OS

The native app, once implemented on a Mac, shares the same backend
contract, so the network testing is already done.
