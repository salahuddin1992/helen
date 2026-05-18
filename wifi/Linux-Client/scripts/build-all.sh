#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "${here}/../../CommClient-Desktop"
[[ -d node_modules ]] || npm ci
echo "==> building all Linux formats"
npx electron-builder --linux AppImage deb rpm tar.gz
ls -lh release/
