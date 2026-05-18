#!/usr/bin/env bash
set -euo pipefail

PURGE=0
for a in "$@"; do
    case "$a" in --purge) PURGE=1;; esac
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2; exit 1
fi

systemctl stop helen-admin-headless.service || true
systemctl disable helen-admin-headless.service || true
rm -f /etc/systemd/system/helen-admin-headless.service
rm -f /usr/bin/helen-admin
rm -f /usr/share/applications/helen-admin.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/helen-admin.svg
rm -rf /opt/helen/admin

systemctl daemon-reload

if [[ ${PURGE} -eq 1 ]]; then
    rm -rf /var/lib/helen-admin /var/log/helen-admin
    id -u helen-admin &>/dev/null && userdel helen-admin || true
fi
echo "Helen-Admin removed."
