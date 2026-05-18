#!/usr/bin/env bash
# =============================================================================
# Helen Mobile (Flutter) — Android build pipeline
# =============================================================================
#
# Builds:
#   - app-debug.apk          (developer artifact)
#   - app-release.apk        (signed APK for sideload / kiosk install)
#   - app-release.aab        (Play Store / private store bundle)
#   - mapping.txt            (R8 deobfuscation map)
#   - sha256sums.txt         (artifact checksums)
#
# Usage:
#   scripts/build_android.sh                # debug + release
#   scripts/build_android.sh debug          # only debug
#   scripts/build_android.sh release        # only release
#   scripts/build_android.sh release --no-sign   # unsigned release
#
# Environment:
#   HELEN_ANDROID_KEYSTORE_PATH    Path to .jks keystore
#   HELEN_ANDROID_KEYSTORE_PASS    Keystore password
#   HELEN_ANDROID_KEY_ALIAS        Key alias (default: helen)
#   HELEN_ANDROID_KEY_PASS         Key password
#   HELEN_FLUTTER_BUILD_NAME       Build name override (default from pubspec)
#   HELEN_FLUTTER_BUILD_NUMBER     Build number override (default: epoch)
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

MODE="${1:-both}"
NO_SIGN="${2:-}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$PROJECT_ROOT/build/android-release-$TS"
KEYSTORE_PATH="${HELEN_ANDROID_KEYSTORE_PATH:-$PROJECT_ROOT/android/keys/helen.jks}"
KEY_ALIAS="${HELEN_ANDROID_KEY_ALIAS:-helen}"
BUILD_NUMBER="${HELEN_FLUTTER_BUILD_NUMBER:-$(date +%s)}"

echo "==> Helen Mobile Android build"
echo "    mode        : $MODE"
echo "    timestamp   : $TS"
echo "    out_dir     : $OUT_DIR"
echo "    keystore    : $KEYSTORE_PATH"
echo "    build_no    : $BUILD_NUMBER"

mkdir -p "$OUT_DIR"

# -----------------------------------------------------------------------------
# Sanity checks
# -----------------------------------------------------------------------------

if ! command -v flutter >/dev/null 2>&1; then
    echo "ERROR: 'flutter' not in PATH" >&2
    exit 2
fi
flutter --version
echo "==> flutter doctor"
flutter doctor -v | tail -30

# -----------------------------------------------------------------------------
# Fetch dependencies + codegen
# -----------------------------------------------------------------------------

echo "==> flutter pub get"
flutter pub get

if grep -qE "build_runner" pubspec.yaml; then
    echo "==> codegen (build_runner)"
    flutter pub run build_runner build --delete-conflicting-outputs
fi

# -----------------------------------------------------------------------------
# l10n
# -----------------------------------------------------------------------------

if [ -f "l10n.yaml" ]; then
    echo "==> flutter gen-l10n"
    flutter gen-l10n
fi

# -----------------------------------------------------------------------------
# Static analysis & tests
# -----------------------------------------------------------------------------

echo "==> flutter analyze"
flutter analyze --no-fatal-infos --no-fatal-warnings || {
    echo "WARN: analyze produced issues; continuing"
}

if [ "${HELEN_SKIP_TESTS:-0}" != "1" ]; then
    echo "==> flutter test"
    flutter test --reporter=expanded || {
        echo "ERROR: tests failed"
        exit 3
    }
fi

# -----------------------------------------------------------------------------
# Sign config
# -----------------------------------------------------------------------------

build_signing_args=""
if [ "$MODE" != "debug" ] && [ "$NO_SIGN" != "--no-sign" ]; then
    if [ ! -f "$KEYSTORE_PATH" ]; then
        echo "ERROR: keystore not found at $KEYSTORE_PATH"
        echo "       to skip signing, pass --no-sign as second arg"
        echo "       to generate one:"
        echo "           keytool -genkey -v -keystore $KEYSTORE_PATH \\"
        echo "               -keyalg RSA -keysize 4096 -validity 36500 \\"
        echo "               -alias $KEY_ALIAS"
        exit 4
    fi
    : "${HELEN_ANDROID_KEYSTORE_PASS:?HELEN_ANDROID_KEYSTORE_PASS not set}"
    : "${HELEN_ANDROID_KEY_PASS:?HELEN_ANDROID_KEY_PASS not set}"
    export HELEN_ANDROID_KEYSTORE_PATH="$KEYSTORE_PATH"
    export HELEN_ANDROID_KEY_ALIAS="$KEY_ALIAS"
fi

# -----------------------------------------------------------------------------
# Build
# -----------------------------------------------------------------------------

if [ "$MODE" = "debug" ] || [ "$MODE" = "both" ]; then
    echo "==> Building APK (debug)..."
    flutter build apk --debug --build-number="$BUILD_NUMBER"
    cp build/app/outputs/flutter-apk/app-debug.apk "$OUT_DIR/helen-debug.apk"
fi

if [ "$MODE" = "release" ] || [ "$MODE" = "both" ]; then
    echo "==> Building APK (release)..."
    flutter build apk --release \
        --build-number="$BUILD_NUMBER" \
        --obfuscate --split-debug-info=build/symbols \
        --split-per-abi
    for abi in armeabi-v7a arm64-v8a x86_64; do
        src="build/app/outputs/flutter-apk/app-$abi-release.apk"
        if [ -f "$src" ]; then
            cp "$src" "$OUT_DIR/helen-$abi.apk"
        fi
    done

    echo "==> Building App Bundle (release)..."
    flutter build appbundle --release \
        --build-number="$BUILD_NUMBER" \
        --obfuscate --split-debug-info=build/symbols
    cp build/app/outputs/bundle/release/app-release.aab "$OUT_DIR/helen-release.aab"

    # Mapping for crash deobfuscation
    if [ -d build/symbols ]; then
        tar czf "$OUT_DIR/symbols.tar.gz" -C build symbols
    fi
fi

# -----------------------------------------------------------------------------
# Checksums + metadata
# -----------------------------------------------------------------------------

echo "==> Computing SHA-256 checksums..."
(cd "$OUT_DIR" && sha256sum * > sha256sums.txt)

cat > "$OUT_DIR/build-info.json" <<EOF
{
    "platform": "android",
    "mode": "$MODE",
    "timestamp": "$TS",
    "build_number": $BUILD_NUMBER,
    "flutter_version": "$(flutter --version | head -1)",
    "git_commit": "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)",
    "git_branch": "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)",
    "signed": $([ "$NO_SIGN" = "--no-sign" ] && echo false || echo true)
}
EOF

echo ""
echo "============================================================"
echo "BUILD OK"
echo "  artifacts: $OUT_DIR"
ls -lh "$OUT_DIR"
echo "============================================================"
