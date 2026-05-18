#!/usr/bin/env bash
# backup.sh — Snapshot Helen-Server data and config to a timestamped tarball.
# Designed for cron / systemd timer use.
#
# Usage:
#   bash backup.sh [output_dir]
# Default output_dir: /var/backups/helen
set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/helen-server}
OUT_DIR=${1:-/var/backups/helen}
KEEP_DAYS=${KEEP_DAYS:-14}

if [ ! -d "$INSTALL_DIR" ]; then
  echo "[ERROR] $INSTALL_DIR not found"
  exit 1
fi

mkdir -p "$OUT_DIR"

TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUT_DIR/helen-backup-$TS.tar.gz"

# Pause briefly via SQLite WAL checkpoint so the backup is consistent.
# The tar capture happens on a copy, so we don't need to stop the service.
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

DATA="$INSTALL_DIR/_internal/data"
if [ -d "$DATA" ]; then
  # Use SQLite's online backup if available — atomic and lock-free.
  if command -v sqlite3 >/dev/null && [ -f "$DATA/commclient.db" ]; then
    sqlite3 "$DATA/commclient.db" ".backup '$TMPDIR/commclient.db'" 2>/dev/null || \
      cp -a "$DATA/commclient.db" "$TMPDIR/commclient.db"
  fi
fi

# Capture .env, data/, logs/. Avoid the binary tree — that's reproducible
# from the tarball. Backups are for state, not for redistributable code.
tar czf "$OUT" \
  --exclude="*.pyc" \
  --exclude="__pycache__" \
  -C "$INSTALL_DIR" \
  $( [ -f "$INSTALL_DIR/.env" ] && echo ".env" ) \
  $( [ -d "$INSTALL_DIR/_internal/data" ] && echo "_internal/data" ) \
  $( [ -d "$INSTALL_DIR/logs" ] && echo "logs" )

# Substitute the freshly checkpointed DB if we made one
if [ -f "$TMPDIR/commclient.db" ]; then
  # Re-create the tarball with the consistent DB swapped in
  # (simpler than tar --transform since tarball is small)
  STAGE=$(mktemp -d)
  trap 'rm -rf "$TMPDIR" "$STAGE"' EXIT
  tar xzf "$OUT" -C "$STAGE"
  cp -a "$TMPDIR/commclient.db" "$STAGE/_internal/data/commclient.db"
  tar czf "$OUT" -C "$STAGE" .
fi

echo "[+] Backup written: $OUT ($(du -h "$OUT" | cut -f1))"

# Rotate — delete tarballs older than $KEEP_DAYS
find "$OUT_DIR" -name "helen-backup-*.tar.gz" -mtime +$KEEP_DAYS -delete -print
