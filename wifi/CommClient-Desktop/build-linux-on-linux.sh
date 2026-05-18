#!/usr/bin/env bash
# Build the AppImage / deb / rpm on a real Linux box (cross-build from
# Windows can't produce AppImage because mksquashfs is a Linux ELF that
# Windows can't execute, even via WSL inside Git Bash).
#
# Usage on Linux/WSL:
#   chmod +x build-linux-on-linux.sh
#   ./build-linux-on-linux.sh
#
# Outputs:
#   release/Helen Desktop-1.0.0.AppImage
#   release/helen-desktop_1.0.0_amd64.deb
#   release/helen-desktop-1.0.0.x86_64.rpm
#   release/commclient-desktop-1.0.0.tar.gz
set -euo pipefail
cd "$(dirname "$0")"

echo "→ ensuring deps are present"
for cmd in node npm fakeroot dpkg-deb rpmbuild; do
  command -v "$cmd" >/dev/null || echo "  ⚠ missing: $cmd (install via apt/dnf — see README)"
done

echo "→ npm install"
npm install --no-audit --no-fund

echo "→ npm run prebuild + build:renderer"
npm run prebuild
npm run build:renderer

echo "→ electron-builder --linux"
./node_modules/.bin/electron-builder --linux --config electron-builder.yml

echo "✓ done — artifacts in release/"
ls -la release/*.AppImage release/*.deb release/*.rpm release/*.tar.gz 2>/dev/null || true
