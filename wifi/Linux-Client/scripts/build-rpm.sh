#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "${here}/../../CommClient-Desktop"
[[ -d node_modules ]] || npm ci
echo "==> building .rpm (Linux x64)"
npx electron-builder --linux rpm
ls -lh release/*.rpm 2>/dev/null || true
