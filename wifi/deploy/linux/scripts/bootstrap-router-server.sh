#!/usr/bin/env bash
# bootstrap-router-server.sh — Install Helen-Router AND Helen-Server on
# the same host with a shared token, automatically wiring them together.
#
# After this finishes:
#   - Both services are running under systemd
#   - The server is configured with HELEN_REQUIRE_ROUTER=1
#   - The server auto-registers with the router via /router/register
#   - mDNS advertises the router as `_helen-router._tcp.local`
#   - Clients on the LAN can find the router via mDNS or UDP scan
#   - Direct connections to the server (port 3000) are blocked unless
#     they carry the shared token
#
# Usage:
#   sudo bash bootstrap-router-server.sh \
#        helen-server-linux-1.0.0.tar.gz \
#        helen-router-linux-1.0.0.tar.gz
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root: sudo bash $0 <server-tar> <router-tar>"
  exit 1
fi

SERVER_TAR="${1:?usage: bootstrap-router-server.sh <server-tar> <router-tar>}"
ROUTER_TAR="${2:?usage: bootstrap-router-server.sh <server-tar> <router-tar>}"

[ -f "$SERVER_TAR" ] || { echo "Server tarball not found: $SERVER_TAR"; exit 1; }
[ -f "$ROUTER_TAR" ] || { echo "Router tarball not found: $ROUTER_TAR"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "============================================="
echo "  Helen Bootstrap — Router + Server (linked)"
echo "============================================="

# 1. Generate the shared token
TOKEN=$(openssl rand -hex 32)
echo "[*] Generated shared token (will appear in both .env files)"

# 2. Install the router first — server needs it to register against
echo
echo "[1/2] Installing Helen-Router..."
HELEN_ROUTER_TOKEN="$TOKEN" \
  bash "$SCRIPT_DIR/install-router.sh" "$ROUTER_TAR"

# Override .env with our shared token and pre-set default upstream
ROUTER_ENV=/opt/helen-router/.env
{
  echo "HELEN_ROUTER_TOKEN=$TOKEN"
  echo "HELEN_ROUTER_HOST=0.0.0.0"
  echo "HELEN_ROUTER_PORT=8080"
} > "$ROUTER_ENV"
chown helen:helen "$ROUTER_ENV"
chmod 600 "$ROUTER_ENV"
systemctl restart helen-router

# 3. Install the server with router-required mode + auto-register
echo
echo "[2/2] Installing Helen-Server..."
bash "$SCRIPT_DIR/install-server.sh" "$SERVER_TAR"

SERVER_ENV=/opt/helen-server/.env
# Append router config to server's .env (preserves JWT_SECRET that the
# server installer already generated).
{
  echo "HELEN_REQUIRE_ROUTER=1"
  echo "HELEN_ROUTER_TOKEN=$TOKEN"
  echo "HELEN_ROUTER_URL=http://127.0.0.1:8080"
} >> "$SERVER_ENV"
chown helen:helen "$SERVER_ENV"
chmod 600 "$SERVER_ENV"
systemctl restart helen-server

# 4. Wait for both to come up
echo
echo "[*] Waiting for services to become healthy..."
for i in $(seq 1 30); do
  router_ok=$(curl -sf -o /dev/null -w "%{http_code}" http://localhost:8080/router/health 2>/dev/null || echo 000)
  server_ok=$(curl -sf -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null || echo 000)
  if [ "$router_ok" = "200" ] && [ "$server_ok" = "200" ]; then
    break
  fi
  sleep 2
done

echo
echo "============================================="
echo "  Bootstrap result"
echo "============================================="
echo
echo "  Router:    http://$(hostname -I | awk '{print $1}'):8080  ($router_ok)"
echo "  Server:    http://$(hostname -I | awk '{print $1}'):3000  ($server_ok)"
echo "  Token:     /opt/helen-router/.env  (shared)"
echo
echo "  Verify the chain:"
echo "    curl http://localhost:8080/router/upstreams      # server should appear"
echo "    curl http://localhost:8080/api/health            # via router → 200"
echo "    curl http://localhost:3000/api/auth/login -X POST -d '{}'  # → 403 (good!)"
echo
echo "  Logs:"
echo "    journalctl -u helen-router -f"
echo "    journalctl -u helen-server -f"
echo
