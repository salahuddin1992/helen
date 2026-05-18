# Build Apple artifacts without a Mac

The three Apple-only deliverables — **iOS Native Swift `.ipa`**, **iOS via
Capacitor `.ipa`**, and **macOS `Helen Desktop.dmg`** — cannot be built on
Windows or Linux because they require Apple's `xcodebuild`, `codesign`,
`hdiutil`, and `pod` toolchain. There is no cross-platform substitute.

This repo includes a **GitHub Actions workflow that builds all three on a
free GitHub-hosted macOS runner**, so you don't need a developer Mac on
hand. Push the repo to GitHub once and every build is one click away.

---

## One-time setup

1. **Create a GitHub repo** (public or private — either works for free
   macOS minutes):
   ```bash
   # On github.com → "New repository" → name it (e.g. "helen")
   # Don't initialize with README / .gitignore — we already have those.
   ```

2. **Push this directory** to GitHub:
   ```bash
   cd C:/Users/youse/c/wifi
   git init
   git add .
   git commit -m "Initial Helen monorepo import"
   git branch -M main
   git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
   git push -u origin main
   ```

3. (Optional) **Add signing secrets** for distribution-grade artifacts.
   In your repo, go to **Settings → Secrets and variables → Actions** and
   add these five secrets. Without them the workflow still builds, but
   produces unsigned ad-hoc artifacts only.

   | Secret | What it is |
   |---|---|
   | `APPLE_DEVELOPER_TEAM_ID`    | 10-char team ID (e.g. `ABCDE12345`) |
   | `APPLE_CERTIFICATE_BASE64`   | `base64 -i Certificates.p12` of your iOS Distribution + Developer ID Application combined `.p12` |
   | `APPLE_CERTIFICATE_PASSWORD` | password used when exporting the `.p12` |
   | `APPLE_NOTARY_APPLE_ID`      | Apple ID email used by `notarytool` |
   | `APPLE_NOTARY_PASSWORD`      | app-specific password for the Apple ID |

---

## Triggering a build

Open your repo on GitHub → **Actions** tab → **Apple Builds (iOS + macOS)**
→ **Run workflow** → pick a `build_mode` (`dev` / `beta` / `prod` for the
desktop) → **Run workflow**.

Or push a tag like `v1.0.0` and all three jobs run automatically:
```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## Downloading artifacts

When the run finishes (≈15–25 minutes for all three jobs in parallel), a
green checkmark appears. Open the run page; at the bottom you'll see:

| Artifact | Contents |
|---|---|
| `ios-native-swift`        | `HelenApp-1.0.0.ipa` + `HelenApp-1.0.0.xcarchive.zip` |
| `ios-capacitor`           | `Helen-Mobile-1.0.0.xcarchive.zip` |
| `macos-desktop-<mode>`    | `Helen Desktop-1.0.0.dmg` (+ `.zip`, `.app`) |

Click each name to download. Artifacts are retained for 30 days.

---

## Local fallback (if you ever get a Mac)

The same builds work locally on macOS:

```bash
# iOS Native Swift
cd iOS/HelenApp
brew install xcodegen
xcodegen generate
open HelenApp.xcodeproj    # Xcode handles the build

# iOS via Capacitor
cd CommClient-Mobile
chmod +x build-ios-on-mac.sh
./build-ios-on-mac.sh

# macOS Desktop
cd CommClient-Desktop
chmod +x build-mac-on-mac.sh
./build-mac-on-mac.sh dev|beta|prod
```

---

## Why Mac is unavoidable

Apple requires its toolchain to run on a genuine Mac (real or VM). The
critical bits — `codesign`, `hdiutil`, `productbuild`, `notarytool`,
CocoaPods's iOS hooks, and Xcode itself — only ship for macOS. There is
no licensed cross-compile path. GitHub-hosted macOS runners are the
free and supported way to build these from a non-Mac dev machine.
