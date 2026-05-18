#!/usr/bin/env bash
# install-router.sh — One-shot installer for Helen-Router on Linux.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root: sudo bash $0"
  exit 1
fi

INSTALL_DIR=${INSTALL_DIR:-/opt/helen-router}
TARBALL=${1:-helen-router-linux-1.0.0.tar.gz}

if [ ! -f "$TARBALL" ]; then
  echo "[ERROR] Tarball not found: $TARBALL"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "============================================="
echo "  Helen-Router installer"
echo "  Target: $INSTALL_DIR"
echo "============================================="

if ! id helen >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin helen || \
    useradd -r -s /bin/false helen
fi

mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/logs"
tar xzf "$TARBALL" --strip-components=1 -C "$INSTALL_DIR"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  TOKEN=$(openssl rand -hex 32)
  cat > "$INSTALL_DIR/.env" <<EOF
HELEN_ROUTER_TOKEN=$TOKEN
HELEN_ROUTER_HOST=0.0.0.0
HELEN_ROUTER_PORT=8080
EOF
  chmod 600 "$INSTALL_DIR/.env"
  echo "[*] Generated HELEN_ROUTER_TOKEN — share it with every Helen-Server."
fi

chown -R helen:helen "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/Helen-Router" 2>/dev/null || true

cp "$TOOLKIT_ROOT/systemd/helen-router.service" /etc/systemd/system/
systemctl daemon-reload

# Allow port 8080 from RFC1918
if [ -x "$SCRIPT_DIR/setup-firewall.sh" ]; then
  # Reuse the LAN_RANGES helper from setup-firewall.sh — but for one extra port
  for r in 127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
    if command -v ufw >/dev/null && ufw status | grep -q "active"; then
      ufw allow from "$r" to any port 8080 proto tcp comment "Helen-Router HTTP" >/dev/null 2>&1 || true
    elif command -v firewall-cmd >/dev/null; then
      firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$r port protocol=tcp port=8080 accept" >/dev/null 2>&1 || true
    elif command -v iptables >/dev/null; then
      iptables -C INPUT -p tcp -s "$r" --dport 8080 -j ACCEPT 2>/dev/null || \
      iptables -A INPUT -p tcp -s "$r" --dport 8080 -j ACCEPT
    fi
  done
  command -v firewall-cmd >/dev/null && firewall-cmd --reload >/dev/null 2>&1 || true
fi

systemctl enable helen-router
systemctl restart helen-router

sleep 3
systemctl --no-pager status helen-router | head -15 || true

echo
echo "============================================="
echo "  ✅ Helen-Router installed."
echo "============================================="
echo
echo "  Config:  $INSTALL_DIR/.env  (TOKEN auto-generated)"
echo "  Logs:    journalctl -u helen-router -f"
echo "  Health:  curl http://localhost:8080/router/health"
echo
echo "  Next steps:"
echo "    1. Copy HELEN_ROUTER_TOKEN value from $INSTALL_DIR/.env"
echo "    2. On every Helen-Server, set in its .env:"
echo "         HELEN_REQUIRE_ROUTER=1"
echo "         HELEN_ROUTER_TOKEN=<same value>"
echo "    3. Register the server with the router (or set"
echo "         HELEN_ROUTER_DEFAULT_UPSTREAM=http://<server-ip>:3000"
echo "       in router's .env for static fallback)."
echo
