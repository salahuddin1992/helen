#!/usr/bin/env bash
# Helen-Client installer — Linux.
# Auto-detects distro and picks the right package format.
#
# Usage: sudo ./install.sh [--appimage | --deb | --rpm]
set -euo pipefail

FORMAT=""
for a in "$@"; do
    case "$a" in
        --appimage) FORMAT=appimage;;
        --deb)      FORMAT=deb;;
        --rpm)      FORMAT=rpm;;
        --help|-h)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2; exit 1
fi

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
RELEASE_DIR="$(cd "${here}/../CommClient-Desktop/release" 2>/dev/null && pwd || true)"

if [[ -z "${RELEASE_DIR}" || ! -d "${RELEASE_DIR}" ]]; then
    echo "no release artifacts found — run ./scripts/build-<format>.sh first" >&2
    exit 3
fi

# Detect distro if format not specified
if [[ -z "${FORMAT}" ]]; then
    if command -v dpkg >/dev/null 2>&1; then
        FORMAT=deb
    elif command -v rpm >/dev/null 2>&1; then
        FORMAT=rpm
    else
        FORMAT=appimage
    fi
    echo "==> auto-detected format: ${FORMAT}"
fi

case "${FORMAT}" in
    deb)
        pkg="$(ls -1 "${RELEASE_DIR}"/*.deb 2>/dev/null | head -1 || true)"
        [[ -z "${pkg}" ]] && { echo "no .deb in ${RELEASE_DIR}"; exit 4; }
        echo "==> installing ${pkg}"
        apt install -y "${pkg}"
        ;;
    rpm)
        pkg="$(ls -1 "${RELEASE_DIR}"/*.rpm 2>/dev/null | head -1 || true)"
        [[ -z "${pkg}" ]] && { echo "no .rpm in ${RELEASE_DIR}"; exit 4; }
        echo "==> installing ${pkg}"
        if command -v dnf >/dev/null 2>&1; then dnf install -y "${pkg}"
        else rpm -i --replacepkgs "${pkg}"; fi
        ;;
    appimage)
        img="$(ls -1 "${RELEASE_DIR}"/*.AppImage 2>/dev/null | head -1 || true)"
        [[ -z "${img}" ]] && { echo "no .AppImage in ${RELEASE_DIR}"; exit 4; }
        echo "==> installing ${img} → /opt/helen/client"
        install -d /opt/helen/client
        cp -f "${img}" /opt/helen/client/Helen.AppImage
        chmod +x /opt/helen/client/Helen.AppImage
        # Launcher shim
        cat > /usr/bin/helen <<'EOF'
#!/bin/sh
exec /opt/helen/client/Helen.AppImage "$@"
EOF
        chmod +x /usr/bin/helen
        # .desktop entry
        install -m 0644 "${here}/desktop/helen.desktop" \
            /usr/share/applications/helen.desktop
        if [[ -f "${here}/desktop/helen.svg" ]]; then
            install -m 0644 "${here}/desktop/helen.svg" \
                /usr/share/icons/hicolor/scalable/apps/helen.svg
            gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
        fi
        update-desktop-database /usr/share/applications 2>/dev/null || true
        ;;
esac

echo "==> installing helen-client-ctl"
install -o root -g root -m 0755 \
    "${here}/bin/helen-client-ctl" /usr/bin/helen-client-ctl 2>/dev/null || true

echo "==> installed. Launch from app menu or 'helen' in terminal."
echo "    utility: helen-client-ctl {start|diag|reset|doctor}"
