#!/usr/bin/env bash
# install-rendezvous.sh — One-shot installer for Helen-Rendezvous on Linux.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root: sudo bash $0"
  exit 1
fi

INSTALL_DIR=${INSTALL_DIR:-/opt/helen-rendezvous}
TARBALL=${1:-helen-rendezvous-linux-1.0.1.tar.gz}

if [ ! -f "$TARBALL" ]; then
  echo "[ERROR] Tarball not found: $TARBALL"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "============================================="
echo "  Helen-Rendezvous installer"
echo "  Target: $INSTALL_DIR"
echo "============================================="

# Reuse the helen user if already created by install-server.sh
if ! id helen >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin helen || \
    useradd -r -s /bin/false helen
fi

mkdir -p "$INSTALL_DIR"
tar xzf "$TARBALL" --strip-components=2 -C "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/logs"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  TOKEN=$(openssl rand -hex 32)
  cat > "$INSTALL_DIR/.env" <<EOF
HELEN_RENDEZVOUS_TOKEN=$TOKEN
HELEN_RENDEZVOUS_HOST=0.0.0.0
HELEN_RENDEZVOUS_PORT=9090
HELEN_RELAY_BACKEND_PORT=9101
HELEN_RELAY_FRONTEND_PORT=9102
EOF
  chmod 600 "$INSTALL_DIR/.env"
fi

chown -R helen:helen "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/Helen-Rendezvous" || true

cp "$TOOLKIT_ROOT/systemd/helen-rendezvous.service" /etc/systemd/system/
systemctl daemon-reload

if [ -x "$SCRIPT_DIR/setup-firewall.sh" ]; then
  bash "$SCRIPT_DIR/setup-firewall.sh" rendezvous || \
    echo "[!] Firewall setup skipped — open ports 9090/9101/9102 manually."
fi

systemctl enable helen-rendezvous
systemctl restart helen-rendezvous

sleep 3
systemctl --no-pager status helen-rendezvous | head -15 || true

echo
echo "============================================="
echo "  ✅ Helen-Rendezvous installed."
echo "============================================="
echo
echo "  Config:  $INSTALL_DIR/.env  (TOKEN auto-generated)"
echo "  Logs:    journalctl -u helen-rendezvous -f"
echo "  Health:  curl http://localhost:9090/health"
echo
