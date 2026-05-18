#!/usr/bin/env bash
# Quick 0/non-zero exit health probe, usable by monitoring (Nagios etc.)
# or docker HEALTHCHECK.
set -euo pipefail

PORT="${PORT:-3000}"
HOST="${HOST:-127.0.0.1}"
TIMEOUT="${TIMEOUT:-3}"

if ! command -v curl >/dev/null 2>&1; then
    echo "curl missing" >&2
    exit 3
fi

code="$(curl -s -o /dev/null -w '%{http_code}' \
        --max-time "${TIMEOUT}" \
        "http://${HOST}:${PORT}/api/health" || echo 000)"

case "${code}" in
    200) echo "OK"; exit 0;;
    000) echo "UNREACHABLE"; exit 2;;
    *)   echo "UNHEALTHY http=${code}"; exit 1;;
esac
