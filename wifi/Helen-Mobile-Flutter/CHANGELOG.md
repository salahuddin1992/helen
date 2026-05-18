# Helen Mobile — Changelog

All notable changes to Helen Mobile are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Build pipeline scripts (`scripts/build_android.sh`, `scripts/build_ios.sh`)
- GitHub Actions workflow `.github/workflows/mobile-build.yml`
- `RELEASE.md` operator runbook for release process
- `CHANGELOG.md` (this file)

## [1.0.0] — 2026-05-12

### Added
- Initial Flutter-native client implementation (5,326 LOC of Dart)
- Riverpod-based state management
- `go_router` navigation
- Drift-backed local cache for messages, contacts, calls
- `flutter_secure_storage` for credentials
- WebSocket-driven realtime sync via `socket_io_client`
- Material 3 theming with dynamic color, RTL Arabic + LTR English
- Push notifications (FCM + local notifications)
- Biometric auth via `local_auth`
- JWT decoding + refresh flow
- Comprehensive `lib/` layered architecture (core / data / domain / presentation / providers / router / services)
- Android + iOS native shells
- Integration test scaffold under `integration_test/`

### Status
- Scaffold complete; release binaries pending CI keystore + signing material.
