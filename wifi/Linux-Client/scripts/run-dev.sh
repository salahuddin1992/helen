#!/usr/bin/env bash
# Dev mode — Vite + Electron, no packaging.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "${here}/../../CommClient-Desktop"
[[ -d node_modules ]] || npm ci
exec npm run dev
