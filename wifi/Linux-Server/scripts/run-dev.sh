#!/usr/bin/env bash
# Run Helen-Server from source (dev mode) — no install, no systemd.
# Data lives in CommClient-Server/data/, not /var/lib/helen.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
SERVER_SRC="$(cd "${here}/../../CommClient-Server" && pwd)"
cd "${SERVER_SRC}"

if [[ ! -d "venv" ]]; then
    python3 -m venv venv
    # shellcheck source=/dev/null
    source venv/bin/activate
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
else
    # shellcheck source=/dev/null
    source venv/bin/activate
fi

export PORT="${PORT:-3000}"
export HELEN_HTTPS_DISABLED="${HELEN_HTTPS_DISABLED:-1}"

echo "dev server on http://0.0.0.0:${PORT}"
exec python run.py
