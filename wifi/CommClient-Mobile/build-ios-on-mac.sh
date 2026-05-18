#!/usr/bin/env bash
# Build the iOS .ipa on a real Mac. iOS builds require Xcode + Apple
# Developer Program membership for distribution; this script handles
# the local development build (debug, no provisioning) only.
#
# Usage on macOS with Xcode installed:
#   chmod +x build-ios-on-mac.sh
#   ./build-ios-on-mac.sh
#
# Outputs:
#   ios/App/build/Helen.ipa
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "✗ This script must run on macOS with Xcode installed."
  exit 1
fi
command -v xcodebuild >/dev/null || { echo "✗ xcodebuild missing — install Xcode."; exit 1; }
command -v pod >/dev/null || { echo "✗ CocoaPods missing — sudo gem install cocoapods"; exit 1; }

echo "→ npm install + sync renderer + cap sync"
npm install --no-audit --no-fund
node scripts/sync-renderer.mjs
npx cap sync ios

echo "→ pod install"
( cd ios/App && pod install )

echo "→ xcodebuild archive"
( cd ios/App && \
  xcodebuild -workspace App.xcworkspace -scheme App \
    -configuration Debug -destination "generic/platform=iOS Simulator" \
    -derivedDataPath build build )

echo "✓ done — open in Xcode for distribution / IPA export:"
echo "   open ios/App/App.xcworkspace"
