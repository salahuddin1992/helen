#!/usr/bin/env bash
# ============================================================
# Helen Desktop — macOS build (signed/unsigned, .app + .dmg)
# ============================================================
# Run this on macOS. electron-builder refuses to build for macOS
# from any other OS because it must invoke /usr/bin/codesign,
# /usr/bin/hdiutil, and /usr/bin/SetFile from Apple's toolchain.
#
# Modes:
#   dev   — unsigned .app for local testing (default)
#   beta  — signed .app + .dmg for ad-hoc distribution (needs Developer ID)
#   prod  — signed + notarized .dmg for direct download (needs notary creds)
#
# Usage:
#   chmod +x build-mac-on-mac.sh
#   ./build-mac-on-mac.sh dev
#   APPLE_ID=... APPLE_APP_PASSWORD=... APPLE_TEAM_ID=... ./build-mac-on-mac.sh prod
#
# Outputs:
#   release/mac/Helen Desktop.app
#   release/Helen Desktop-1.0.0.dmg     (beta + prod)
#   release/Helen Desktop-1.0.0-mac.zip (prod, for Sparkle/auto-update)

set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-dev}"

# ── Pre-flight checks ────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    echo "✗ This script must run on macOS. Detected: $(uname)"
    echo "  electron-builder cannot cross-build for macOS from other"
    echo "  hosts because it depends on /usr/bin/codesign + hdiutil."
    exit 1
fi

command -v node >/dev/null || { echo "✗ Node.js missing — brew install node"; exit 1; }
command -v xcrun >/dev/null || { echo "✗ Xcode Command-Line Tools missing — xcode-select --install"; exit 1; }

if [[ "$MODE" != "dev" ]]; then
    if ! security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
        echo "✗ No Developer ID Application certificate found in keychain."
        echo "  For ad-hoc/distribution builds, install one from Apple Developer Portal:"
        echo "  https://developer.apple.com/account/resources/certificates"
        exit 1
    fi
fi

# ── Install + build ───────────────────────────────────────────
echo "→ npm install"
npm install --no-audit --no-fund

echo "→ npm run prebuild + build:renderer"
npm run prebuild
npm run build:renderer

# ── Configure target based on mode ────────────────────────────
case "$MODE" in
    dev)
        echo "→ DEV build (unsigned .app, no DMG, no notarisation)"
        export CSC_IDENTITY_AUTO_DISCOVERY=false
        TARGET="dir"
        ;;
    beta)
        echo "→ BETA build (signed .app + DMG, no notarisation)"
        TARGET="dmg"
        ;;
    prod)
        echo "→ PROD build (signed + notarised DMG)"
        : "${APPLE_ID:?must export APPLE_ID for notarisation}"
        : "${APPLE_APP_PASSWORD:?must export APPLE_APP_PASSWORD (app-specific)}"
        : "${APPLE_TEAM_ID:?must export APPLE_TEAM_ID}"
        TARGET="dmg"
        ;;
    *)
        echo "✗ unknown mode: $MODE (use dev | beta | prod)"
        exit 1
        ;;
esac

# Patch electron-builder.yml on the fly to add the requested target.
# Restored via trap on exit so subsequent runs aren't polluted.
BACKUP="$(mktemp)"
cp electron-builder.yml "$BACKUP"
trap 'mv "$BACKUP" electron-builder.yml' EXIT

# Replace the mac target line.
node -e "
const fs = require('fs');
const yml = require('js-yaml');
const cfg = yml.load(fs.readFileSync('electron-builder.yml', 'utf-8'));
cfg.mac = cfg.mac || {};
cfg.mac.target = [{ target: '$TARGET', arch: ['x64', 'arm64'] }];
if ('$MODE' === 'dev') {
    cfg.mac.identity = null;
    cfg.mac.hardenedRuntime = false;
    cfg.mac.gatekeeperAssess = false;
} else {
    cfg.mac.hardenedRuntime = true;
    cfg.mac.gatekeeperAssess = true;
    cfg.mac.entitlements = 'build/entitlements.mac.plist';
    cfg.mac.entitlementsInherit = 'build/entitlements.mac.plist';
}
fs.writeFileSync('electron-builder.yml', yml.dump(cfg));
" || { echo "✗ failed to patch electron-builder.yml"; exit 1; }

# Make sure we have js-yaml available for the patch step.
[ -d node_modules/js-yaml ] || npm install --no-save js-yaml >/dev/null

# ── Build ─────────────────────────────────────────────────────
echo "→ electron-builder --mac"
./node_modules/.bin/electron-builder --mac --config electron-builder.yml

# ── Notarisation (prod only) ─────────────────────────────────
if [[ "$MODE" == "prod" ]]; then
    DMG=$(ls -1 release/*.dmg | head -1)
    [ -z "$DMG" ] && { echo "✗ no DMG produced"; exit 1; }
    echo "→ submitting $DMG for notarisation..."
    xcrun notarytool submit "$DMG" \
        --apple-id "$APPLE_ID" \
        --password "$APPLE_APP_PASSWORD" \
        --team-id  "$APPLE_TEAM_ID" \
        --wait
    echo "→ stapling notarisation ticket..."
    xcrun stapler staple "$DMG"
fi

# ── Summary ───────────────────────────────────────────────────
echo
echo "✓ Build complete"
echo "  Outputs:"
ls -la release/mac*/ release/*.dmg release/*.zip 2>/dev/null || true
echo
case "$MODE" in
    dev)  echo "  Open:    open 'release/mac/Helen Desktop.app'" ;;
    beta) echo "  Distribute the .dmg via direct download or AirDrop." ;;
    prod) echo "  Notarised .dmg ready for production distribution." ;;
esac
