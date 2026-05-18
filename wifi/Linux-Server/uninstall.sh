#!/usr/bin/env bash
# Helen-Server uninstaller.
# Default: removes service + binaries, PRESERVES /var/lib/helen data.
# Pass --purge to wipe data too.
set -euo pipefail

PURGE=0
for a in "$@"; do
    case "$a" in
        --purge) PURGE=1;;
        --help|-h) echo "usage: $0 [--purge]"; exit 0;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2
    exit 1
fi

echo "==> stopping service"
systemctl stop helen-server.service || true
systemctl disable helen-server.service || true

echo "==> removing files"
rm -f /etc/systemd/system/helen-server.service
rm -f /usr/bin/helen-server
rm -f /etc/logrotate.d/helen-server
rm -rf /opt/helen/server
rm -rf /run/helen

systemctl daemon-reload

if [[ ${PURGE} -eq 1 ]]; then
    echo "==> --purge: wiping data + logs + config"
    rm -rf /var/lib/helen /var/log/helen /etc/helen
    if id -u helen &>/dev/null; then userdel helen || true; fi
    if getent group helen &>/dev/null; then groupdel helen || true; fi
    echo "done. full teardown."
else
    echo "data preserved:  /var/lib/helen"
    echo "logs preserved:  /var/log/helen"
    echo "config preserved: /etc/helen"
    echo "(re-run with --purge to wipe everything)"
fi
