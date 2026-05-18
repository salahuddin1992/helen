#!/usr/bin/env bash
# build-linux-wsl.sh — Build Helen-Server Linux ELF directly inside
# the active WSL Ubuntu distro (no Docker needed). Source files live
# on the Windows side at /mnt/c/...; we copy them into /tmp/ to dodge
# Windows ACL friction during pip install.

set -euo pipefail

PROJECT=/mnt/c/Users/youse/c/wifi/CommClient-Server
WORK=/tmp/helen-linux-build

cd "$PROJECT"

echo "[*] Staging source into $WORK ..."
rm -rf "$WORK"
mkdir -p "$WORK"
cp -a app run.py CommClient-Server.spec requirements.txt "$WORK/"
for d in admin iOS iOS-Admin certs Vault hub admin-secret; do
    [ -d "$d" ] && cp -a "$d" "$WORK/"
done

cd "$WORK"

echo "[*] Building venv + installing deps (this is one-shot per WSL distro)..."
if [ ! -d /tmp/helen-linux-venv ]; then
    apt-get update -qq
    apt-get install -y -qq python3-venv python3-pip gcc binutils libffi-dev libssl-dev
    python3 -m venv /tmp/helen-linux-venv
    /tmp/helen-linux-venv/bin/pip install --quiet --upgrade pip
fi
/tmp/helen-linux-venv/bin/pip install --quiet -r requirements.txt
/tmp/helen-linux-venv/bin/pip install --quiet pyinstaller

echo "[*] Running PyInstaller..."
/tmp/helen-linux-venv/bin/pyinstaller --noconfirm --clean \
    --distpath dist-linux \
    --workpath build-linux \
    --name Helen-Server \
    --onedir \
    --add-data "app/transports/config:app/transports/config" \
    $([ -d admin ] && echo "--add-data admin:admin") \
    $([ -d iOS ] && echo "--add-data iOS:iOS") \
    $([ -d iOS-Admin ] && echo "--add-data iOS-Admin:iOS-Admin") \
    $([ -d Vault ] && echo "--add-data Vault:Vault") \
    $([ -d hub ] && echo "--add-data hub:hub") \
    $([ -d admin-secret ] && echo "--add-data admin-secret:admin-secret") \
    $([ -d certs ] && echo "--add-data certs:certs") \
    --hidden-import=app \
    --hidden-import=zeroconf \
    --hidden-import=psutil \
    --hidden-import=aiosqlite \
    --hidden-import=sqlalchemy.dialects.sqlite \
    --hidden-import=sqlalchemy.dialects.sqlite.aiosqlite \
    --hidden-import=sqlalchemy.dialects.sqlite.pysqlite \
    --hidden-import=ldap3 \
    --collect-submodules sqlalchemy \
    --collect-submodules aiosqlite \
    run.py

echo "[*] Stripping shared objects..."
find dist-linux/Helen-Server -name '*.so*' -exec strip --strip-unneeded {} + 2>/dev/null || true

echo "[*] Cleaning seed data + cython sources..."
rm -rf dist-linux/Helen-Server/_internal/zeroconf/*.c 2>/dev/null || true
DATA="dist-linux/Helen-Server/_internal/data"
if [ -d "$DATA" ]; then
    rm -f "$DATA"/{commclient.db,metrics_history.sqlite*,replicated_state.sqlite*,control_plane_audit.ndjson,overlay_state.json,resilience_retry_queue.jsonl,service_registry.json,topology.json,secret_master_code.txt,vault_master_code.txt}
    rm -rf "$DATA"/{backups,uploads,avatars}/* 2>/dev/null || true
fi

echo "[*] Copying back to Windows side..."
DEST="$PROJECT/dist-linux"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a dist-linux/Helen-Server "$DEST/"

echo "[*] Building tarball..."
cd "$PROJECT"
rm -f helen-server-linux-1.0.0.tar.gz
tar -C dist-linux -czf "$PROJECT/../helen-server-linux-1.0.0.tar.gz" Helen-Server/

echo "[+] Linux build done."
du -sh "$DEST/Helen-Server" "$PROJECT/../helen-server-linux-1.0.0.tar.gz" 2>/dev/null || true
