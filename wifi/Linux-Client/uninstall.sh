#!/usr/bin/env bash
set -euo pipefail

PURGE=0
for a in "$@"; do
    case "$a" in --purge) PURGE=1;; esac
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2; exit 1
fi

# Try package managers first
if dpkg -l helen &>/dev/null;  then apt remove -y helen   || true; fi
if rpm -q helen &>/dev/null;   then
    if command -v dnf &>/dev/null; then dnf remove -y helen || true
    else rpm -e helen || true; fi
fi

# Manual-install cleanup
rm -f /usr/bin/helen
rm -f /usr/share/applications/helen.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/helen.svg
rm -rf /opt/helen/client

if [[ ${PURGE} -eq 1 ]]; then
    # Per-user data — wipe across ALL users' home dirs
    for home in /home/*; do
        [[ -d "${home}/.config/Helen" ]] && rm -rf "${home}/.config/Helen"
        [[ -d "${home}/.cache/Helen" ]]  && rm -rf "${home}/.cache/Helen"
    done
fi

update-desktop-database /usr/share/applications 2>/dev/null || true
echo "Helen client removed."
