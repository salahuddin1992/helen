#!/usr/bin/env bash
# =============================================================================
# Helen Mobile (Flutter) — iOS build pipeline
# =============================================================================
#
# Builds:
#   - Helen.app                (debug runner)
#   - Helen.ipa                (signed release for TestFlight / Ad Hoc / Enterprise)
#   - Helen.xcarchive          (archive for App Store submission)
#
# Requires macOS host (Xcode + Apple Developer account or Enterprise certs).
#
# Usage:
#   scripts/build_ios.sh                              # release ipa
#   scripts/build_ios.sh debug                        # debug only
#   scripts/build_ios.sh adhoc                        # adhoc distribution
#   scripts/build_ios.sh app-store                    # app-store distribution
#
# Environment:
#   HELEN_IOS_TEAM_ID              Apple Developer Team ID
#   HELEN_IOS_BUNDLE_ID            Bundle identifier (default: com.helen.mobile)
#   HELEN_IOS_EXPORT_OPTIONS_PLIST Path to ExportOptions.plist (defaults to ios/ExportOptions-<method>.plist)
#   HELEN_IOS_PROVISIONING_PROFILE Provisioning profile name (optional)
#   HELEN_FLUTTER_BUILD_NUMBER     Build number override (default: epoch)
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

MODE="${1:-app-store}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$PROJECT_ROOT/build/ios-release-$TS"
BUILD_NUMBER="${HELEN_FLUTTER_BUILD_NUMBER:-$(date +%s)}"
BUNDLE_ID="${HELEN_IOS_BUNDLE_ID:-com.helen.mobile}"

echo "==> Helen Mobile iOS build"
echo "    mode        : $MODE"
echo "    timestamp   : $TS"
echo "    out_dir     : $OUT_DIR"
echo "    bundle_id   : $BUNDLE_ID"
echo "    build_no    : $BUILD_NUMBER"

mkdir -p "$OUT_DIR"

# -----------------------------------------------------------------------------
# Sanity
# -----------------------------------------------------------------------------

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: iOS builds require macOS"
    exit 2
fi
if ! command -v flutter >/dev/null; then echo "ERROR: flutter not in PATH"; exit 2; fi
if ! command -v xcodebuild >/dev/null; then echo "ERROR: xcodebuild not in PATH"; exit 2; fi
flutter --version
xcodebuild -version

echo "==> flutter pub get"
flutter pub get

if grep -qE "build_runner" pubspec.yaml; then
    echo "==> codegen"
    flutter pub run build_runner build --delete-conflicting-outputs
fi

if [ -f "l10n.yaml" ]; then
    flutter gen-l10n
fi

# -----------------------------------------------------------------------------
# CocoaPods
# -----------------------------------------------------------------------------

echo "==> pod install"
(cd ios && pod install --repo-update)

# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

if [ "${HELEN_SKIP_TESTS:-0}" != "1" ]; then
    echo "==> flutter test"
    flutter test --reporter=expanded || { echo "ERROR: tests failed"; exit 3; }
fi

# -----------------------------------------------------------------------------
# Export options resolution
# -----------------------------------------------------------------------------

case "$MODE" in
    debug)
        echo "==> Building debug (no archive)"
        flutter build ios --debug --no-codesign --build-number="$BUILD_NUMBER"
        cp -r build/ios/iphoneos/Runner.app "$OUT_DIR/Helen-debug.app"
        exit 0
        ;;
    adhoc)
        export_method="ad-hoc"
        ;;
    enterprise)
        export_method="enterprise"
        ;;
    app-store|appstore)
        export_method="app-store"
        ;;
    *)
        echo "ERROR: unknown mode '$MODE'"
        exit 2
        ;;
esac

EXPORT_PLIST="${HELEN_IOS_EXPORT_OPTIONS_PLIST:-$PROJECT_ROOT/ios/ExportOptions-${export_method}.plist}"

if [ ! -f "$EXPORT_PLIST" ]; then
    echo "INFO: generating ExportOptions plist at $EXPORT_PLIST"
    : "${HELEN_IOS_TEAM_ID:?HELEN_IOS_TEAM_ID not set}"
    mkdir -p "$(dirname "$EXPORT_PLIST")"
    cat > "$EXPORT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>$export_method</string>
    <key>teamID</key>
    <string>$HELEN_IOS_TEAM_ID</string>
    <key>uploadBitcode</key>
    <false/>
    <key>uploadSymbols</key>
    <true/>
    <key>compileBitcode</key>
    <false/>
    <key>signingStyle</key>
    <string>automatic</string>
    <key>stripSwiftSymbols</key>
    <true/>
    <key>destination</key>
    <string>export</string>
</dict>
</plist>
PLIST
fi

# -----------------------------------------------------------------------------
# Build archive
# -----------------------------------------------------------------------------

ARCHIVE_PATH="$OUT_DIR/Helen.xcarchive"

echo "==> flutter build ipa"
flutter build ipa \
    --release \
    --build-number="$BUILD_NUMBER" \
    --export-method="$export_method" \
    --export-options-plist="$EXPORT_PLIST" \
    --obfuscate --split-debug-info=build/symbols

# Flutter creates artifacts under build/ios/ipa and build/ios/archive
if [ -d "build/ios/archive/Runner.xcarchive" ]; then
    cp -R "build/ios/archive/Runner.xcarchive" "$ARCHIVE_PATH"
fi
if ls build/ios/ipa/*.ipa >/dev/null 2>&1; then
    cp build/ios/ipa/*.ipa "$OUT_DIR/Helen.ipa"
fi

# dSYMs (crash deobfuscation)
if [ -d "build/ios/archive/Runner.xcarchive/dSYMs" ]; then
    tar czf "$OUT_DIR/dSYMs.tar.gz" -C "build/ios/archive/Runner.xcarchive" dSYMs
fi
if [ -d build/symbols ]; then
    tar czf "$OUT_DIR/symbols.tar.gz" -C build symbols
fi

# -----------------------------------------------------------------------------
# Checksums + metadata
# -----------------------------------------------------------------------------

(cd "$OUT_DIR" && shasum -a 256 * > sha256sums.txt)

cat > "$OUT_DIR/build-info.json" <<EOF
{
    "platform": "ios",
    "mode": "$MODE",
    "export_method": "$export_method",
    "timestamp": "$TS",
    "build_number": $BUILD_NUMBER,
    "bundle_id": "$BUNDLE_ID",
    "team_id": "${HELEN_IOS_TEAM_ID:-unknown}",
    "flutter_version": "$(flutter --version | head -1)",
    "git_commit": "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
}
EOF

echo ""
echo "============================================================"
echo "BUILD OK"
echo "  artifacts: $OUT_DIR"
ls -lh "$OUT_DIR"
echo "============================================================"
echo ""
echo "Upload to TestFlight / App Store:"
echo "  xcrun altool --upload-app \\"
echo "      -f $OUT_DIR/Helen.ipa \\"
echo "      -t ios \\"
echo "      -u <apple-id> -p <app-specific-password>"
