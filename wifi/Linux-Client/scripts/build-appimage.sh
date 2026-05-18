#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "${here}/../../CommClient-Desktop"

if [[ ! -d node_modules ]]; then
    npm ci
fi
echo "==> building AppImage (Linux x64)"
npx electron-builder --linux AppImage
echo "output:"
ls -lh release/*.AppImage 2>/dev/null || true
