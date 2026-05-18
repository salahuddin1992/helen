#!/usr/bin/env bash
# Helen-Router Linux ELF build — runs inside WSL Ubuntu 22.04.
set -euo pipefail

PROJECT=/mnt/c/Users/youse/c/wifi/Helen-Router
WORK=/tmp/helen-router-build

cd "$PROJECT"

echo "[*] Staging source into $WORK ..."
rm -rf "$WORK"
mkdir -p "$WORK"
cp -a app run.py Helen-Router.spec requirements.txt installer-icon.ico LICENSE.txt "$WORK/"

cd "$WORK"

echo "[*] Reusing the helen-server venv if present (saves a fresh pip install)..."
if [ ! -d /tmp/helen-linux-venv ]; then
    apt-get update -qq
    apt-get install -y -qq python3-venv python3-pip gcc binutils libffi-dev libssl-dev
    python3 -m venv /tmp/helen-linux-venv
    /tmp/helen-linux-venv/bin/pip install --quiet --upgrade pip
fi
/tmp/helen-linux-venv/bin/pip install --quiet -r requirements.txt
/tmp/helen-linux-venv/bin/pip install --quiet pyinstaller

echo "[*] PyInstaller..."
/tmp/helen-linux-venv/bin/pyinstaller --noconfirm --clean \
    --distpath dist-linux \
    --workpath build-linux \
    --name Helen-Router \
    --onedir \
    --hidden-import=app \
    --hidden-import=app.main \
    --hidden-import=app.mesh \
    --hidden-import=zeroconf \
    --hidden-import=psutil \
    --collect-submodules fastapi \
    --collect-submodules starlette \
    --collect-submodules uvicorn \
    --collect-submodules httpx \
    --collect-submodules websockets \
    --collect-submodules structlog \
    --collect-submodules zeroconf \
    --collect-data zeroconf \
    run.py

echo "[*] Stripping shared objects..."
find dist-linux/Helen-Router -name '*.so*' -exec strip --strip-unneeded {} + 2>/dev/null || true

echo "[*] Copying back to Windows side..."
DEST="$PROJECT/dist-linux"
rm -rf "$DEST" 2>/dev/null || true
mkdir -p "$DEST"
cp -a dist-linux/Helen-Router "$DEST/"

echo "[*] Building tarball..."
cd "$PROJECT"
rm -f helen-router-linux-1.0.0.tar.gz
tar -C dist-linux -czf "$PROJECT/../helen-router-linux-1.0.0.tar.gz" Helen-Router/

echo "[+] Done."
du -sh "$DEST/Helen-Router" "$PROJECT/../helen-router-linux-1.0.0.tar.gz" 2>/dev/null || true
