#!/usr/bin/env bash
# install-server.sh — One-shot installer for Helen-Server on Linux.
# Supports systemd-based distros (Ubuntu, Debian, Fedora, RHEL, Arch).
# Run as root.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root: sudo bash $0"
  exit 1
fi

INSTALL_DIR=${INSTALL_DIR:-/opt/helen-server}
TARBALL=${1:-helen-server-linux-1.0.0.tar.gz}

if [ ! -f "$TARBALL" ]; then
  echo "[ERROR] Tarball not found: $TARBALL"
  echo "        Pass it as the first arg: bash $0 /path/to/helen-server-linux-1.0.0.tar.gz"
  exit 1
fi

# Locate this script's directory so we can pick up the systemd unit + firewall script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "============================================="
echo "  Helen-Server installer"
echo "  Target: $INSTALL_DIR"
echo "============================================="

# 1. Create dedicated user (non-login)
if ! id helen >/dev/null 2>&1; then
  echo "[*] Creating system user 'helen'..."
  useradd --system --no-create-home --shell /usr/sbin/nologin helen || \
    useradd -r -s /bin/false helen
fi

# 2. Extract tarball
echo "[*] Extracting $TARBALL..."
mkdir -p "$INSTALL_DIR"
tar xzf "$TARBALL" --strip-components=1 -C "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/_internal/data" "$INSTALL_DIR/logs"

# 3. Generate .env if missing
if [ ! -f "$INSTALL_DIR/.env" ]; then
  echo "[*] Generating .env with random JWT_SECRET..."
  JWT_SECRET=$(openssl rand -hex 32)
  cat > "$INSTALL_DIR/.env" <<EOF
JWT_SECRET=$JWT_SECRET
PORT=3000
HTTPS_PORT=3443
DEBUG=0
EOF
  chmod 600 "$INSTALL_DIR/.env"
fi

# 4. Permissions
chown -R helen:helen "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/Helen-Server" || true

# 5. systemd unit
echo "[*] Installing systemd unit..."
cp "$TOOLKIT_ROOT/systemd/helen-server.service" /etc/systemd/system/
systemctl daemon-reload

# 6. Firewall (best-effort; non-fatal if firewall manager absent)
if [ -x "$SCRIPT_DIR/setup-firewall.sh" ]; then
  bash "$SCRIPT_DIR/setup-firewall.sh" server || \
    echo "[!] Firewall setup skipped — open ports 3000/3443/41234 manually."
fi

# 7. Enable + start
echo "[*] Enabling and starting helen-server..."
systemctl enable helen-server
systemctl restart helen-server

sleep 3
systemctl --no-pager status helen-server | head -15 || true

echo
echo "============================================="
echo "  ✅ Helen-Server installed."
echo "============================================="
echo
echo "  Config:    $INSTALL_DIR/.env"
echo "  Logs:      journalctl -u helen-server -f"
echo "  Health:    curl http://localhost:3000/api/health"
echo "  Stop:      systemctl stop helen-server"
echo "  Disable:   systemctl disable helen-server"
echo "  Uninstall: bash $SCRIPT_DIR/uninstall-server.sh"
echo
