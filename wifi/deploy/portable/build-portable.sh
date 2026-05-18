#!/usr/bin/env bash
# build-portable.sh — Assembles a self-contained portable USB image
# with every Helen artefact + scripts. The output dir can be copied
# straight onto a USB stick / external drive / network share.
set -euo pipefail

# Resolve project root (this script lives in deploy/portable/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT="${1:-$ROOT/dist-portable}"

echo "[*] Project root: $ROOT"
echo "[*] Output:       $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"/{windows,linux,macos,android,desktop,scripts,docs}

echo "[*] Stage Windows installers..."
cp "$ROOT/CommClient-Server/Helen-Server-Setup-1.0.0.exe"        "$OUT/windows/" 2>/dev/null || true
cp "$ROOT/CommClient-Server/Helen-Admin-Setup-1.0.0.exe"         "$OUT/windows/" 2>/dev/null || true
cp "$ROOT/Helen-Rendezvous/Helen-Rendezvous-Setup-1.0.0.exe"     "$OUT/windows/" 2>/dev/null || true
cp "$ROOT/CommClient-Desktop/release/Helen Desktop Setup 1.0.0.exe" "$OUT/windows/" 2>/dev/null || true

echo "[*] Stage Linux tarballs..."
cp "$ROOT/helen-server-linux-1.0.0.tar.gz"        "$OUT/linux/" 2>/dev/null || true
cp "$ROOT/helen-server-1.0.0.docker.tar"          "$OUT/linux/" 2>/dev/null || true
cp "$ROOT/helen-rendezvous-linux-1.0.2.tar.gz"    "$OUT/linux/" 2>/dev/null || true
cp "$ROOT/helen-admin-linux-1.0.0.tar.gz"         "$OUT/linux/" 2>/dev/null || true

echo "[*] Stage macOS bundle..."
cp "$ROOT/helen-server-macos-1.0.0.tar.gz" "$OUT/macos/" 2>/dev/null || true

echo "[*] Stage Android APKs..."
cp "$ROOT/CommClient-Mobile/Helen-Mobile-1.0.0-release.apk" "$OUT/android/" 2>/dev/null || true
cp "$ROOT/CommClient-Mobile/Helen-Mobile-1.0.0-debug.apk"   "$OUT/android/" 2>/dev/null || true
cp "$ROOT/CommClient-Mobile/Helen-Mobile-1.0.0.aab"         "$OUT/android/" 2>/dev/null || true

echo "[*] Stage Desktop client packages (Linux/Mac)..."
cp "$ROOT/CommClient-Desktop/release/Helen Desktop-1.0.0.AppImage"   "$OUT/desktop/" 2>/dev/null || true
cp "$ROOT/CommClient-Desktop/release/commclient-desktop_1.0.0_amd64.deb" "$OUT/desktop/" 2>/dev/null || true
cp "$ROOT/CommClient-Desktop/release/commclient-desktop-1.0.0.tar.gz"   "$OUT/desktop/" 2>/dev/null || true

echo "[*] Stage Web PWA..."
mkdir -p "$OUT/web"
cp -r "$ROOT/CommClient-Web/dist"/* "$OUT/web/" 2>/dev/null || true

echo "[*] Stage scripts..."
cp "$ROOT/deploy/linux/scripts/install-server.sh"      "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/install-rendezvous.sh"  "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/setup-firewall.sh"      "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/uninstall-server.sh"    "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/health-check.sh"        "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/health-check.ps1"       "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/backup.sh"              "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/backup.ps1"             "$OUT/scripts/"
cp "$ROOT/deploy/linux/scripts/restore.sh"             "$OUT/scripts/"
cp "$ROOT/CommClient-Server/tools/self-sign-helen.ps1" "$OUT/scripts/"
cp -r "$ROOT/deploy/linux/systemd"                     "$OUT/scripts/systemd"
cp -r "$ROOT/deploy/docker"                            "$OUT/scripts/docker"
cp -r "$ROOT/deploy/ansible"                           "$OUT/scripts/ansible"
cp    "$ROOT/CommClient-Server/installer-icon.ico"     "$OUT/installer-icon.ico"

echo "[*] Stage launchers..."
cp "$SCRIPT_DIR/AUTORUN.INF"   "$OUT/"
cp "$SCRIPT_DIR/Launcher.cmd"  "$OUT/"
cp "$SCRIPT_DIR/Launcher.sh"   "$OUT/"
chmod +x "$OUT/Launcher.sh" "$OUT"/scripts/*.sh

echo "[*] Stage docs..."
cp "$ROOT/DEPLOY-GUIDE.md"      "$OUT/docs/" 2>/dev/null || true
cp "$ROOT/DELIVERY-MANIFEST.md" "$OUT/docs/" 2>/dev/null || true
cp "$ROOT/SECURITY-ARCHITECTURE.md" "$OUT/docs/" 2>/dev/null || true

# Top-level README that explains the layout
cat > "$OUT/README.txt" <<'EOF'
Helen LAN Suite - Portable Deployment Image
============================================

This directory is self-contained. Copy it to a USB stick or network
share and run the launcher for your platform:

  Windows:     Launcher.cmd  (or insert USB - autorun)
  Linux/macOS: ./Launcher.sh

Layout:
  windows/     Helen-Server, Admin, Rendezvous, Desktop installers
  linux/       Helen-Server tarball, Docker image, Rendezvous tarball
  macos/       Helen-Server source bundle (Intel + Apple Silicon)
  android/     APK + AAB
  desktop/     Helen Desktop client packages (Linux + tarball)
  web/         Helen Web PWA (host with: python3 -m http.server)
  scripts/     install / health / backup / firewall scripts
  scripts/systemd/   systemd unit files
  scripts/docker/    docker compose stack
  scripts/ansible/   Ansible playbook for multi-host rollout
  docs/        DEPLOY-GUIDE, SECURITY, DELIVERY manifest

Helen is 100% LAN-only. Nothing in this suite phones home.

Quick start (Windows headless server):
  1. Run Launcher.cmd
  2. Choose [1] Helen-Server
  3. Choose components: Server + Service + Firewall
  4. (Optional) Choose [5] to self-sign and silence SmartScreen
  5. Verify with [6] Run health check

Quick start (Linux headless server):
  sudo bash scripts/install-server.sh linux/helen-server-linux-1.0.0.tar.gz

Quick start (Docker):
  cp scripts/docker/.env.example scripts/docker/.env
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 32)|" scripts/docker/.env
  cd scripts/docker && docker compose up -d

Bulk rollout to many hosts:
  cd scripts/ansible
  cp inventory.example.ini inventory.ini  # edit it
  ansible-playbook -i inventory.ini site.yml

EOF

# Manifest with sizes
( cd "$OUT" && find . -type f -printf "%s\t%p\n" | sort -k2 ) > "$OUT/MANIFEST.txt"

TOTAL=$(du -sh "$OUT" | cut -f1)
echo
echo "============================================="
echo "  Portable image ready: $OUT"
echo "  Total size:            $TOTAL"
echo "============================================="
echo
echo "  To create a USB image:"
echo "    cd \"$OUT\""
echo "    cp -r . /Volumes/USB-DRIVE/      # macOS"
echo "    cp -r . /media/\$USER/USB/        # Linux"
echo "    xcopy /E /I . D:\\               # Windows (D: = USB)"
echo
echo "  Or zip it for distribution:"
echo "    cd \"$OUT/..\" && tar czf helen-portable-1.0.0.tar.gz dist-portable/"
echo
