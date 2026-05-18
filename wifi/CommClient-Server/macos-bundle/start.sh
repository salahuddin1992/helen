#!/usr/bin/env bash
# Helen-Server — macOS launcher
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
  echo "[ERROR] venv غير موجود. شغّل ./install.sh أولاً."
  exit 1
fi

# shellcheck disable=SC1091
source venv/bin/activate

# Load .env
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "============================================="
echo "  Helen-Server starting..."
echo "  HTTP:  http://0.0.0.0:${PORT:-3000}"
echo "  HTTPS: https://0.0.0.0:${HTTPS_PORT:-3443}"
echo "  mDNS:  _helen-server._tcp"
echo "  UDP discovery: 41234"
echo "============================================="

exec python run.py
