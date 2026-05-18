# Helen Mobile — Flutter Client

A production-grade Flutter-native mobile client for the **Helen / CommClient** platform.
Replacement-grade alternative to the existing Capacitor-based `CommClient-Mobile`. The
Capacitor app is left **100% untouched** — this project lives in its own directory.

---

## Architecture

```
lib/
  app.dart                 # MaterialApp.router + theming
  main.dart                # entry point + ProviderScope
  router/                  # go_router config + typed routes + auth guard
  core/
    config/                # env, constants
    logger/                # structured logger
    errors/                # AppException hierarchy + global handler
    storage/               # secure storage + Drift DB
  data/
    api/                   # Dio-based REST clients (auth, messages, files, calls, pairing, oauth)
    socket/                # socket_io_client wrapper + typed events
    models/                # freezed data models (User, Channel, Message, Call, AuthTokens)
  domain/
    repositories/          # Repository contracts
    usecases/              # Use cases (login, send_message, upload_file)
  presentation/
    screens/               # Screens (auth, pairing, channels, messages, calls, settings)
    widgets/               # Shared widgets (loading, error, empty)
    theme/                 # Material 3 dark + light
  providers/               # Riverpod providers
  services/                # Platform services (push, permissions, mDNS, webrtc, biometrics)
  l10n/                    # ARB files (Arabic + English)

android/                   # Android platform (com.helen.mobile)
ios/                       # iOS platform (helen:// URL scheme)
test/                      # Unit + widget tests
integration_test/          # Full-flow tests
```

---

## Server Integration

The mobile client connects to the same CommClient-Server backend used by the
Electron desktop client. Endpoint conventions match `CommClient-Desktop/src/renderer/services/api.client.ts`.

- **Auth**: `POST /api/auth/login`, `/register`, `/refresh`, `/logout` (matches `app/api/routes/auth.py`)
- **Pairing (Module O)**: `POST /api/pairing/v2/start`, `/complete`, `/poll`
- **OAuth (Module N)**: `GET /api/oauth/providers`, `/authorize/{provider}`, `/callback/{provider}`
- **Channels & Messages**: `GET/POST /api/channels`, `/messages`
- **Files**: `POST /api/files/upload`, resumable chunks via `/api/files/resumable/*`
- **Calls**: `POST /api/calls/init`, `/ice-candidates`
- **Socket.IO**: `wss://server/socket.io` with bearer auth in connect query

---

## Setup

```bash
# 1. install Flutter 3.24+ and Dart 3.5+
flutter --version

# 2. install deps
flutter pub get

# 3. run codegen (freezed, riverpod, drift, json_serializable)
flutter pub run build_runner build --delete-conflicting-outputs

# 4. configure environment
cp .env.example .env
# edit .env: HELEN_SERVER_URL=http://192.168.x.x:3000 etc.

# 5. run on a device
flutter devices
flutter run --debug
```

### Configuration (.env)

```ini
HELEN_SERVER_URL=http://192.168.1.10:3000
HELEN_SOCKET_URL=ws://192.168.1.10:3000
HELEN_OAUTH_REDIRECT=helen://oauth/callback
HELEN_PAIRING_REDIRECT=helen://pair/callback
HELEN_LOG_LEVEL=debug
```

---

## Build

```bash
# Android debug APK
flutter build apk --debug

# Android release APK (split per ABI for smaller binaries)
flutter build apk --release --split-per-abi

# Android App Bundle for Play Store
flutter build appbundle --release

# iOS release (requires macOS + Xcode)
flutter build ios --release --no-codesign
```

---

## Testing

```bash
# Unit + widget tests
flutter test

# With coverage
flutter test --coverage
genhtml coverage/lcov.info -o coverage/html

# Integration test (real device or emulator)
flutter test integration_test/login_flow_test.dart
```

---

## Notes

- **E2EE is intentionally out of scope** for this module; it would integrate with the
  Vault subsystem separately.
- The Capacitor `CommClient-Mobile/` app is preserved untouched per project requirements.
- Push notifications: Android uses FCM; iOS uses APNs through `firebase_messaging`.
- LAN discovery uses mDNS (`multicast_dns` package) and probes `_helen._tcp.local`.
- Custom URL scheme `helen://` is registered on both platforms for OAuth + pairing callbacks.
