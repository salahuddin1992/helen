# Helen Mobile — Release Pipeline

Helen Mobile is a Flutter-native client for the Helen / CommClient platform.
This document describes how releases are built, signed, and distributed.

## 1. Versioning

Helen Mobile uses [Semantic Versioning 2.0](https://semver.org/).

The version lives in `pubspec.yaml`:

```yaml
version: 1.0.0+1
```

- `1.0.0` — semantic version (`MAJOR.MINOR.PATCH`)
- `+1` — build number, monotonically increasing (Play Store / App Store requirement)

The `scripts/build_*.sh` scripts override the build number to `$(date +%s)` to
guarantee monotonic increase, so each CI build produces a unique build number
without manual bumping.

## 2. Branching

| Branch | Purpose |
|--------|---------|
| `main` | Latest stable; CI builds signed release on every merge |
| `release/<version>` | Per-release stabilization branches |
| `feature/<name>` | Feature work (PR target: `main`) |
| `hotfix/<issue>` | Hotfixes against `release/<version>` |

Tag releases as `mobile-v1.0.0` from the `release/1.0.0` branch.

## 3. Local builds

### Android

```bash
cd Helen-Mobile-Flutter
export HELEN_ANDROID_KEYSTORE_PATH=$HOME/.helen/helen.jks
export HELEN_ANDROID_KEYSTORE_PASS=<keystore-password>
export HELEN_ANDROID_KEY_PASS=<key-password>
scripts/build_android.sh release
```

Outputs under `build/android-release-<timestamp>/`:

- `helen-arm64-v8a.apk`, `helen-armeabi-v7a.apk`, `helen-x86_64.apk` — per-ABI APKs (sideload friendly)
- `helen-release.aab` — App Bundle (Play Store / Aurora Store / private store)
- `symbols.tar.gz` — split debug info for crash deobfuscation
- `sha256sums.txt` — artifact checksums
- `build-info.json` — build metadata

### iOS (macOS only)

```bash
cd Helen-Mobile-Flutter
export HELEN_IOS_TEAM_ID=ABCDE12345
export HELEN_IOS_BUNDLE_ID=com.helen.mobile
scripts/build_ios.sh app-store    # or: adhoc, enterprise, debug
```

Outputs under `build/ios-release-<timestamp>/`:

- `Helen.ipa` — signed for the chosen distribution method
- `Helen.xcarchive` — for App Store Connect submission
- `dSYMs.tar.gz` — Apple symbol files for crash deobfuscation
- `symbols.tar.gz` — Dart symbol map
- `sha256sums.txt`
- `build-info.json`

## 4. Keystore management

The Android keystore lives at `android/keys/helen.jks` (gitignored).

Generate once:

```bash
keytool -genkey -v \
    -keystore android/keys/helen.jks \
    -keyalg RSA -keysize 4096 \
    -validity 36500 \
    -alias helen \
    -dname "CN=Helen, OU=Mobile, O=Helen, L=Internal, ST=LAN, C=XX"
```

Back up the keystore + passwords to your operator-managed secrets store.
**Loss of the keystore means no further Play Store updates** — keep it safe.

For CI, base64-encode the keystore and store it as the
`ANDROID_KEYSTORE_BASE64` secret:

```bash
base64 -w0 android/keys/helen.jks > /tmp/keystore.b64
# upload contents to GitHub Actions secret
```

## 5. iOS code signing

Two paths:

### 5.1 Automatic signing (recommended for small teams)

Sign in to Xcode with your Apple Developer account; Xcode manages provisioning
profiles automatically. CI requires:

- `IOS_DIST_CERT_P12` — base64 of a distribution certificate `.p12`
- `IOS_DIST_CERT_PASS` — password for the `.p12`
- `IOS_KEYCHAIN_PASS` — fresh password for the CI keychain
- `IOS_TEAM_ID` — Apple Developer Team ID
- `IOS_BUNDLE_ID` — bundle identifier (default `com.helen.mobile`)

### 5.2 Manual signing with fastlane match (recommended for larger teams)

Run `fastlane match` to manage certificates + profiles via a private git repo.
Then update `scripts/build_ios.sh` to use the matched identity.

## 6. Distribution channels

| Channel | Audience | Pipeline |
|---------|----------|----------|
| **Internal sideload** | Operators, on-prem deployments | APK published to private artifact server; clients install via internal app catalog |
| **Aurora Store / F-Droid** | Privacy-conscious orgs | Submit AAB to private/community store |
| **Google Play (private)** | Enterprise customers | Internal track + closed testing → production |
| **TestFlight** | iOS internal QA | `xcrun altool --upload-app` against Helen.ipa |
| **App Store** | Public iOS users (if applicable) | Same as TestFlight + manual App Store Connect submission |
| **Apple Enterprise Program** | Air-gapped iOS clients | Enterprise `.ipa` deployed via MDM (Jamf / Intune) |

## 7. Decision: Flutter vs Capacitor strategy

Helen ships **two parallel mobile clients**:

1. **CommClient-Mobile (Capacitor)** — wraps the same React/TypeScript renderer
   used by the Electron desktop client. Reuses 100% of the desktop UI code,
   produces signed APK + AAB on every server build. **Status: shipping.**

2. **Helen-Mobile-Flutter** — separate native Flutter codebase. Higher native
   integration potential (background notifications, biometric auth, calling
   from lock screen, CallKit / ConnectionService deep hooks) at the cost of
   maintaining a parallel UI stack.

**Recommendation:** Maintain both. Capacitor for fast iteration + UI parity
with desktop. Flutter for enterprise customers who need deep OS integration
(VoIP push, kiosk mode, custom Android Auto, Apple CarPlay).

Operators choosing a deployment can pick either; both authenticate against the
same Helen Server API and produce identical user experiences for messaging,
calling, file transfer, and presence.

## 8. Pre-release checklist

Before tagging a release:

- [ ] `flutter analyze` passes with zero issues
- [ ] `flutter test` passes
- [ ] `integration_test/` smoke tests run on at least one physical Android + one iOS device
- [ ] Version bumped in `pubspec.yaml`
- [ ] `CHANGELOG.md` updated
- [ ] L10n strings reviewed in both `ar` and `en`
- [ ] In-app license + open-source-licenses screen up-to-date
- [ ] Privacy policy URL accessible from settings
- [ ] Build size baseline check: APK <20MB per-ABI, IPA <40MB

## 9. Post-release verification

After a build is in users' hands:

- [ ] Crash-free sessions ≥99.5% over first 24h (via in-app crash reporter)
- [ ] No spike in `auth.login_failed` events on server side
- [ ] No regression in call setup latency (server-side QoS dashboard)
- [ ] Sentry / crash reports triaged within 4 business hours
- [ ] Roll-back plan tested (previous release still installable from artifact server)

## 10. Roll-back procedure

If a release causes incidents:

1. **Play Store**: halt rollout via Play Console → Manage releases → Halt.
2. **App Store**: Submit hotfix immediately; no in-place rollback possible.
3. **Internal sideload**: Update artifact server to serve previous APK as `latest`.
4. **Enterprise MDM**: Push previous IPA via Jamf / Intune.
5. Communicate via in-app announcement + email to operators within 1 hour.
