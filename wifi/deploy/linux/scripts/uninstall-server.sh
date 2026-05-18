#!/usr/bin/env bash
# uninstall-server.sh — Remove Helen-Server but preserve data + .env by default.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root."
  exit 1
fi

INSTALL_DIR=${INSTALL_DIR:-/opt/helen-server}
PURGE=${PURGE:-0}   # set PURGE=1 to wipe data + .env too

systemctl stop helen-server 2>/dev/null || true
systemctl disable helen-server 2>/dev/null || true
rm -f /etc/systemd/system/helen-server.service
systemctl daemon-reload

if [ "$PURGE" = "1" ]; then
  echo "[*] PURGE=1 — wiping $INSTALL_DIR entirely..."
  rm -rf "$INSTALL_DIR"
else
  echo "[*] Preserving data and .env (set PURGE=1 to delete them)."
  rm -rf "$INSTALL_DIR/Helen-Server" "$INSTALL_DIR/_internal/lib" \
         "$INSTALL_DIR/_internal/python"* 2>/dev/null || true
  # Conservative — only the binary tree is removed; data, .env, logs stay.
fi

echo "[✓] Helen-Server removed."
