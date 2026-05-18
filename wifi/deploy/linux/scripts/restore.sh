#!/usr/bin/env bash
# restore.sh — Restore a Helen-Server backup tarball.
#
# Usage:
#   sudo bash restore.sh /var/backups/helen/helen-backup-20260504-120000.tar.gz
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root."
  exit 1
fi

INSTALL_DIR=${INSTALL_DIR:-/opt/helen-server}
BACKUP=${1:?usage: restore.sh <backup.tar.gz>}

if [ ! -f "$BACKUP" ]; then
  echo "[ERROR] Backup file not found: $BACKUP"
  exit 1
fi

if [ ! -d "$INSTALL_DIR" ]; then
  echo "[ERROR] $INSTALL_DIR not present. Install Helen-Server first."
  exit 1
fi

echo "[*] Stopping helen-server service..."
systemctl stop helen-server 2>/dev/null || true

# Move existing data aside (don't delete — admin may want to roll back)
if [ -d "$INSTALL_DIR/_internal/data" ]; then
  mv "$INSTALL_DIR/_internal/data" \
     "$INSTALL_DIR/_internal/data.pre-restore-$(date +%Y%m%d-%H%M%S)"
fi
if [ -f "$INSTALL_DIR/.env" ]; then
  cp -a "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.pre-restore"
fi

echo "[*] Extracting $BACKUP..."
tar xzf "$BACKUP" -C "$INSTALL_DIR"

# Permissions
chown -R helen:helen "$INSTALL_DIR/_internal/data" "$INSTALL_DIR/.env" "$INSTALL_DIR/logs" 2>/dev/null || true
chmod 600 "$INSTALL_DIR/.env" 2>/dev/null || true

echo "[*] Starting helen-server..."
systemctl start helen-server 2>/dev/null || true

sleep 3
if curl -fsS --max-time 5 http://localhost:3000/api/health >/dev/null 2>&1; then
  echo "[✓] Restore complete. Server responding."
else
  echo "[!] Restore extracted, but health check failed."
  echo "    Check: journalctl -u helen-server -n 50"
fi
