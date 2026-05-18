#!/usr/bin/env bash
# build-linux.sh — Build Helen-Server Linux ELF inside a Docker container.
# Runs PyInstaller against an Ubuntu 22.04 base, ensuring the resulting
# binary is glibc-2.31 compatible (works on Ubuntu 20.04+, Debian 11+,
# RHEL 9+). No internet at runtime — the build itself pulls Python deps.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[*] Building Linux ELF via Docker..."

docker run --rm \
    -v "$(pwd):/build" \
    -w /build \
    --platform linux/amd64 \
    python:3.12-slim-bookworm \
    bash -c "
        set -e
        apt-get update -qq && apt-get install -y -qq --no-install-recommends \
            gcc libffi-dev libssl-dev binutils
        pip install --quiet --no-cache-dir -r requirements.txt
        pip install --quiet --no-cache-dir pyinstaller
        rm -rf build/CommClient-Server-linux dist-linux
        pyinstaller --noconfirm --clean \
            --distpath dist-linux \
            --workpath build/CommClient-Server-linux \
            --name Helen-Server \
            --onedir \
            --add-data 'app/transports/config:app/transports/config' \
            --add-data 'admin:admin' \
            --add-data 'iOS:iOS' \
            --add-data 'iOS-Admin:iOS-Admin' \
            --add-data 'certs:certs' \
            --hidden-import=app \
            --hidden-import=zeroconf \
            --hidden-import=psutil \
            run.py
        # Strip the binary to shave ~30% off the size.
        find dist-linux/Helen-Server -name '*.so*' -exec strip --strip-unneeded {} + 2>/dev/null || true
        chown -R $(id -u):$(id -g) dist-linux build/CommClient-Server-linux 2>/dev/null || true
    "

echo "[*] Cleaning Cython sources from zeroconf..."
find dist-linux/Helen-Server/_internal/zeroconf -name '*.c' -delete 2>/dev/null || true

echo "[*] Removing seeded test data..."
DATA="dist-linux/Helen-Server/_internal/data"
if [ -d "$DATA" ]; then
    rm -f "$DATA"/{commclient.db,metrics_history.sqlite*,replicated_state.sqlite*,control_plane_audit.ndjson,overlay_state.json,resilience_retry_queue.jsonl,service_registry.json,topology.json,secret_master_code.txt,vault_master_code.txt}
    rm -rf "$DATA"/{backups,uploads,avatars}/* 2>/dev/null || true
fi

echo "[*] Packaging tarball..."
cd dist-linux
tar czf "../../helen-server-linux-1.0.0.tar.gz" Helen-Server/
cd ../..

du -sh wifi/CommClient-Server/dist-linux/Helen-Server wifi/helen-server-linux-1.0.0.tar.gz 2>/dev/null \
    || du -sh dist-linux/Helen-Server "../helen-server-linux-1.0.0.tar.gz"

echo "[+] Linux build complete."
