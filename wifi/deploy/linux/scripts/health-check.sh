#!/usr/bin/env bash
# health-check.sh — End-to-end verification for a Helen LAN deployment.
# Checks: server endpoint, rendezvous endpoint, mDNS, UDP discovery,
#         data freshness, log freshness, certs, ports, free disk.
# Returns 0 if all green, non-zero if any check failed.

# Note: deliberately not using `set -e` so we can collect every issue
# in one pass instead of bailing on the first failure.

PASS=0
FAIL=0
WARN=0

# Override via env: SERVER_URL, RENDEZVOUS_URL, ALL_LOCAL=1
SERVER_URL="${SERVER_URL:-http://localhost:3000}"
RENDEZVOUS_URL="${RENDEZVOUS_URL:-http://localhost:9090}"
DATA_DIR="${DATA_DIR:-/opt/helen-server/_internal/data}"
WARN_DISK_PCT="${WARN_DISK_PCT:-85}"

green()  { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
red()    { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
yellow() { printf "  \033[33m!\033[0m %s\n" "$1"; WARN=$((WARN+1)); }
info()   { printf "    %s\n" "$1"; }

section() { printf "\n\033[1m── %s ──\033[0m\n" "$1"; }

# ── 1. Helen-Server endpoint ─────────────────────────────────
section "Helen-Server"
if curl -fsS --max-time 5 "$SERVER_URL/api/health" >/tmp/helen-health.json 2>/dev/null; then
  if grep -q '"status":"ok"' /tmp/helen-health.json; then
    green "endpoint $SERVER_URL/api/health responding"
    info "$(cat /tmp/helen-health.json)"
  else
    red "endpoint reachable but health body unexpected"
    info "$(cat /tmp/helen-health.json | head -c 200)"
  fi
else
  red "endpoint $SERVER_URL/api/health unreachable"
fi

# HTTPS check (non-fatal — cert may be self-signed)
HTTPS_URL="${SERVER_URL/http:/https:}"; HTTPS_URL="${HTTPS_URL/3000/3443}"
if curl -kfsS --max-time 5 "$HTTPS_URL/api/health" >/dev/null 2>&1; then
  green "HTTPS endpoint $HTTPS_URL responding (cert ignored)"
else
  yellow "HTTPS endpoint $HTTPS_URL unreachable (optional)"
fi

# ── 2. Rendezvous endpoint ────────────────────────────────────
section "Helen-Rendezvous"
if curl -fsS --max-time 5 "$RENDEZVOUS_URL/health" >/dev/null 2>&1; then
  green "rendezvous $RENDEZVOUS_URL/health responding"
elif curl -sS --max-time 5 "$RENDEZVOUS_URL/" 2>/dev/null | grep -qi rendezvous; then
  green "rendezvous root responding"
else
  yellow "rendezvous unreachable (run only on hosts with rendezvous installed)"
fi

# ── 3. Open ports ────────────────────────────────────────────
section "Open ports"
check_port() {
  local proto=$1
  local port=$2
  local label=$3
  if command -v ss >/dev/null; then
    if ss -lntu | awk '{print $5}' | grep -qE ":$port$"; then
      green "$label ($proto $port) listening"
    else
      yellow "$label ($proto $port) not listening"
    fi
  elif command -v netstat >/dev/null; then
    if netstat -lntu 2>/dev/null | grep -qE ":$port "; then
      green "$label ($proto $port) listening"
    else
      yellow "$label ($proto $port) not listening"
    fi
  else
    yellow "no ss/netstat available — port check skipped"
  fi
}
check_port tcp 3000 "Helen-Server HTTP"
check_port tcp 3443 "Helen-Server HTTPS"
check_port udp 41234 "Helen UDP discovery"
check_port udp 5353 "mDNS"

# ── 4. Local processes ───────────────────────────────────────
section "Processes"
for proc in Helen-Server Helen-Rendezvous; do
  if pgrep -f "$proc" >/dev/null 2>&1; then
    green "$proc running (pid $(pgrep -f "$proc" | head -1))"
  else
    yellow "$proc not running locally"
  fi
done

# ── 5. systemd unit status (if available) ────────────────────
section "systemd units"
if command -v systemctl >/dev/null; then
  for unit in helen-server helen-rendezvous; do
    if systemctl is-enabled "$unit" >/dev/null 2>&1; then
      state=$(systemctl is-active "$unit" 2>/dev/null || echo unknown)
      if [ "$state" = "active" ]; then
        green "$unit: enabled and active"
      else
        red "$unit: enabled but state=$state"
      fi
    else
      yellow "$unit: not enabled (or not installed)"
    fi
  done
else
  yellow "systemctl not available — skipping unit checks"
fi

# ── 6. Data directory freshness ──────────────────────────────
section "Data integrity"
if [ -d "$DATA_DIR" ]; then
  green "data dir present: $DATA_DIR"
  if [ -f "$DATA_DIR/commclient.db" ]; then
    age_min=$(( ( $(date +%s) - $(stat -c %Y "$DATA_DIR/commclient.db" 2>/dev/null || stat -f %m "$DATA_DIR/commclient.db") ) / 60 ))
    if [ "$age_min" -lt 60 ]; then
      green "commclient.db updated ${age_min} min ago"
    elif [ "$age_min" -lt 1440 ]; then
      yellow "commclient.db last touched ${age_min} min ago"
    else
      yellow "commclient.db idle for $((age_min/60)) hours"
    fi
  else
    yellow "commclient.db not found (server may not have first-run yet)"
  fi
  # Disk usage
  pct=$(df -P "$DATA_DIR" | awk 'NR==2 {print $5}' | tr -d '%')
  if [ "$pct" -gt "$WARN_DISK_PCT" ]; then
    red "disk ${pct}% full at $DATA_DIR (>$WARN_DISK_PCT%)"
  else
    green "disk ${pct}% used at $DATA_DIR"
  fi
else
  yellow "data dir not present locally: $DATA_DIR"
fi

# ── 7. JWT secret strength ───────────────────────────────────
section "Secrets"
ENV_FILE="${DATA_DIR%/_internal/data}/.env"
if [ -f "$ENV_FILE" ]; then
  jwt_val=$(grep -E '^JWT_SECRET=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
  if [ -z "$jwt_val" ]; then
    red "JWT_SECRET missing in $ENV_FILE"
  elif [ "${#jwt_val}" -lt 32 ]; then
    red "JWT_SECRET too short (${#jwt_val} chars, need 32+)"
  elif echo "$jwt_val" | grep -qiE "change|placeholder|todo|secret$|^helen"; then
    red "JWT_SECRET looks like a placeholder"
  else
    green "JWT_SECRET length: ${#jwt_val} chars"
  fi
else
  yellow ".env not found at $ENV_FILE"
fi

# ── 8. Discovery test ────────────────────────────────────────
section "Discovery"
if command -v avahi-browse >/dev/null; then
  found=$(timeout 3 avahi-browse -tr _helen-server._tcp 2>/dev/null | grep -c "_helen-server")
  if [ "$found" -gt 0 ]; then
    green "mDNS: $found Helen-Server instance(s) advertised"
  else
    yellow "mDNS: no Helen-Server seen on the LAN"
  fi
else
  yellow "avahi-browse not installed — install for LAN-wide discovery diagnostics"
fi

# ── Summary ──────────────────────────────────────────────────
echo
echo "============================================="
printf "  Result: \033[32m%d passed\033[0m" "$PASS"
[ "$WARN" -gt 0 ] && printf "  \033[33m%d warnings\033[0m" "$WARN"
[ "$FAIL" -gt 0 ] && printf "  \033[31m%d failures\033[0m" "$FAIL"
echo
echo "============================================="

# Exit non-zero on hard failures only
[ "$FAIL" -eq 0 ]
