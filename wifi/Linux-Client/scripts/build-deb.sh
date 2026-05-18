#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "${here}/../../CommClient-Desktop"
[[ -d node_modules ]] || npm ci
echo "==> building .deb (Linux x64)"
npx electron-builder --linux deb
ls -lh release/*.deb 2>/dev/null || true
