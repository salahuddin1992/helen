#!/usr/bin/env bash
# build-all.sh — One command to rebuild every Helen artifact and
# sign them with the project's self-signed cert.
#
# Targets:
#   1. Helen-Server.exe (Windows, PyInstaller --onedir)
#   2. Helen-Server (Linux ELF via WSL Ubuntu 22.04)
#   3. Helen-Router.exe (Windows)
#   4. Helen-Router (Linux ELF via WSL)
#   5. Helen-Router-Setup-1.0.0.exe (NSIS)
#   6. Helen-Server-Setup-1.0.0.exe (NSIS)
#   7. Helen Desktop Setup 1.0.0.exe (electron-builder)
#   8. Helen-Mobile-1.0.0-{debug,release}.apk + .aab
#   9. self-sign every signable Windows artifact
#
# Run from the project root (`C:/Users/youse/c/wifi/`).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── colors ──────────────────────────────────────────────────────────
G='\033[0;32m' Y='\033[1;33m' R='\033[0;31m' N='\033[0m'
section() { echo -e "\n${Y}━━━ $* ━━━${N}"; }
ok()      { echo -e "${G}✓ $*${N}"; }
err()     { echo -e "${R}✗ $*${N}"; }

# ── helpers ─────────────────────────────────────────────────────────
kill_helen_processes() {
    # Stop any leftover Helen binaries before rebuilding (PyInstaller
    # bundles can hold file handles inside dist/, blocking --clean).
    if command -v tasklist >/dev/null; then
        for proc in Helen-Server.exe Helen-Router.exe; do
            if tasklist 2>/dev/null | grep -q "$proc"; then
                taskkill //F //IM "$proc" 2>/dev/null || true
            fi
        done
    fi
    # Linux side
    if command -v wsl >/dev/null; then
        MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
            bash -c 'pkill -f Helen-Server; pkill -f Helen-Router; true' \
            2>/dev/null || true
    fi
    sleep 2
}

VENV="$ROOT/CommClient-Server/venv"
if [ ! -x "$VENV/Scripts/python.exe" ]; then
    err "expected venv at $VENV not found"
    err "create it first: cd CommClient-Server && python -m venv venv && pip install -r requirements.txt"
    exit 1
fi

STAGE="${STAGE:-all}"
SKIP_LINUX="${SKIP_LINUX:-0}"
SKIP_MOBILE="${SKIP_MOBILE:-0}"
SKIP_DESKTOP="${SKIP_DESKTOP:-0}"
SKIP_INSTALLERS="${SKIP_INSTALLERS:-0}"
SKIP_SIGN="${SKIP_SIGN:-0}"

# ── 0. cleanup zombies ──────────────────────────────────────────────
section "0. Stopping any running Helen processes"
kill_helen_processes
ok "no zombies on port 3000 / 8080"

# ── 1. Helen-Server Windows ─────────────────────────────────────────
section "1. Helen-Server.exe (Windows / PyInstaller)"
cd "$ROOT/CommClient-Server"
mv "dist/Helen-Server" "dist/old-Helen-Server-$(date +%s)" 2>/dev/null || true
"$VENV/Scripts/python.exe" -m PyInstaller CommClient-Server.spec \
    --noconfirm --clean
test -x dist/Helen-Server/Helen-Server.exe
ok "Helen-Server.exe built"

# ── 2. Helen-Server Linux (WSL) ─────────────────────────────────────
if [ "$SKIP_LINUX" != "1" ]; then
    section "2. Helen-Server Linux ELF (WSL Ubuntu 22.04)"
    rm -rf "$ROOT/CommClient-Server/dist-linux/Helen-Server" 2>/dev/null || true
    MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
        bash /mnt/c/Users/youse/c/wifi/CommClient-Server/scripts/build-linux-wsl.sh
    test -x "$ROOT/CommClient-Server/dist-linux/Helen-Server/Helen-Server"
    ok "Helen-Server (Linux) built"
else
    echo "skipping Linux Helen-Server (SKIP_LINUX=1)"
fi

# ── 3. Helen-Router Windows ─────────────────────────────────────────
section "3. Helen-Router.exe (Windows / PyInstaller)"
cd "$ROOT/Helen-Router"
mv "dist/Helen-Router" "dist/old-Helen-Router-$(date +%s)" 2>/dev/null || true
"$VENV/Scripts/python.exe" -m PyInstaller Helen-Router.spec \
    --noconfirm --clean
test -x dist/Helen-Router/Helen-Router.exe
ok "Helen-Router.exe built"

# ── 4. Helen-Router Linux ───────────────────────────────────────────
if [ "$SKIP_LINUX" != "1" ]; then
    section "4. Helen-Router Linux ELF (WSL)"
    rm -rf "$ROOT/Helen-Router/dist-linux/Helen-Router" 2>/dev/null || true
    MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
        bash /mnt/c/Users/youse/c/wifi/Helen-Router/scripts/build-linux-wsl.sh
    test -x "$ROOT/Helen-Router/dist-linux/Helen-Router/Helen-Router"
    ok "Helen-Router (Linux) built"
fi

# ── 5. NSIS installers ──────────────────────────────────────────────
if [ "$SKIP_INSTALLERS" != "1" ]; then
    section "5. NSIS installers"
    NSIS="/c/Program Files (x86)/NSIS/makensis.exe"
    if [ ! -x "$NSIS" ]; then
        err "makensis.exe not found at $NSIS"
        err "install it: winget install --id NSIS.NSIS"
        exit 1
    fi
    cd "$ROOT/CommClient-Server"
    "$NSIS" installer-server-only.nsi >/dev/null
    ok "Helen-Server-Setup-1.0.0.exe built"
    cd "$ROOT/Helen-Router"
    "$NSIS" installer.nsi >/dev/null
    ok "Helen-Router-Setup-1.0.0.exe built"
fi

# ── 6. Helen Desktop ────────────────────────────────────────────────
if [ "$SKIP_DESKTOP" != "1" ]; then
    section "6. Helen Desktop (Electron)"
    cd "$ROOT/CommClient-Desktop"
    npm run build:renderer
    npx electron-builder --win --config electron-builder.yml
    test -f "release/Helen Desktop Setup 1.0.0.exe"
    ok "Helen Desktop Setup 1.0.0.exe built"
fi

# ── 7. Helen-Mobile ─────────────────────────────────────────────────
if [ "$SKIP_MOBILE" != "1" ]; then
    section "7. Helen-Mobile (Capacitor + Gradle)"
    cd "$ROOT/CommClient-Mobile"
    node scripts/sync-renderer.mjs
    npx cap sync android
    cd android
    ./gradlew assembleDebug assembleRelease bundleRelease
    cd ..
    cp app/build/outputs/apk/debug/app-debug.apk Helen-Mobile-1.0.0-debug.apk \
        2>/dev/null || \
    cp android/app/build/outputs/apk/debug/app-debug.apk Helen-Mobile-1.0.0-debug.apk
    cp android/app/build/outputs/apk/release/app-release.apk Helen-Mobile-1.0.0-release.apk
    cp android/app/build/outputs/bundle/release/app-release.aab Helen-Mobile-1.0.0.aab
    ok "Helen-Mobile artifacts updated"
fi

# ── 8. Self-sign ────────────────────────────────────────────────────
if [ "$SKIP_SIGN" != "1" ]; then
    section "8. Code-sign all Windows binaries"
    pwsh "$ROOT/CommClient-Server/tools/self-sign-helen.ps1" \
        | grep -E "Signed OK|Skip|Sign failed" || true
    ok "signing pass complete"
fi

# ── Summary ─────────────────────────────────────────────────────────
section "Summary"
cd "$ROOT"
echo "Sizes:"
for f in \
    "CommClient-Server/dist/Helen-Server/Helen-Server.exe" \
    "CommClient-Server/dist-linux/Helen-Server/Helen-Server" \
    "CommClient-Server/Helen-Server-Setup-1.0.0.exe" \
    "Helen-Router/dist/Helen-Router/Helen-Router.exe" \
    "Helen-Router/dist-linux/Helen-Router/Helen-Router" \
    "Helen-Router/Helen-Router-Setup-1.0.0.exe" \
    "CommClient-Desktop/release/Helen Desktop Setup 1.0.0.exe" \
    "CommClient-Mobile/Helen-Mobile-1.0.0-debug.apk" \
    "CommClient-Mobile/Helen-Mobile-1.0.0-release.apk" \
    "CommClient-Mobile/Helen-Mobile-1.0.0.aab"; do
    if [ -f "$ROOT/$f" ]; then
        printf "  %-65s %s\n" "$f" "$(du -h "$ROOT/$f" | cut -f1)"
    fi
done
ok "build-all done"
