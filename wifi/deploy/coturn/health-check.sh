#!/usr/bin/env bash
# Helen coturn health check. Exits 0 if TURN is reachable + functional,
# non-zero otherwise. Hook into systemd OnFailure or Prometheus blackbox.
#
# Usage:
#   ./health-check.sh                # localhost
#   ./health-check.sh turn.example.com 3478

set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-3478}"

# 1. Port reachable?
if ! timeout 5 bash -c "</dev/tcp/${HOST}/${PORT}" 2>/dev/null; then
    echo "FAIL: ${HOST}:${PORT} not reachable (TCP probe)"
    exit 1
fi

# 2. STUN binding works (synthetic — uses turnutils which ships with coturn)
if ! command -v turnutils_uclient >/dev/null 2>&1; then
    echo "WARN: turnutils_uclient not in PATH — skipping functional probe"
    echo "OK (port reachable, full probe skipped)"
    exit 0
fi

# 3. Allocate + free a relay using known-good test creds. The configured
#    static-auth-secret must let through this short-term cred. If you
#    customized the realm, add -r <realm>.
USERNAME="$(date -u +%s -d '+5 minutes'):helen-health"
SECRET="${HELEN_TURN_SECRET:-}"
if [ -z "$SECRET" ]; then
    echo "WARN: HELEN_TURN_SECRET not set — skipping HMAC probe"
    echo "OK (port reachable, full probe skipped)"
    exit 0
fi
PASSWORD=$(printf '%s' "${USERNAME}" | openssl dgst -binary -sha1 -hmac "${SECRET}" | base64)

if turnutils_uclient -y -u "${USERNAME}" -w "${PASSWORD}" -p "${PORT}" -n 1 -t -T -e "${HOST}" "${HOST}" >/dev/null 2>&1; then
    echo "OK: TURN ${HOST}:${PORT} fully functional (allocated + relayed test packet)"
    exit 0
else
    echo "FAIL: TURN ${HOST}:${PORT} reachable but allocate/relay failed"
    exit 2
fi
