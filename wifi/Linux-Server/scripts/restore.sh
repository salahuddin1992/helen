#!/usr/bin/env bash
# Restore from a backup.sh bundle.
# Safety: current DATADIR is archived to a side path BEFORE restore,
# so an accidentally bad backup can still be undone.
set -euo pipefail

BUNDLE="${1:?usage: $0 <backup.tar.gz>}"
DATADIR="${DATADIR:-/var/lib/helen}"
ETCDIR="${ETCDIR:-/etc/helen}"
SVC="${SVC:-helen-server}"

if [[ $EUID -ne 0 ]]; then echo "must run as root" >&2; exit 1; fi
[[ -f "${BUNDLE}" ]] || { echo "bundle not found: ${BUNDLE}" >&2; exit 2; }

echo "==> stopping ${SVC}"
systemctl stop "${SVC}" || true

side="/var/backups/helen/restore-savepoint-$(date +%s)"
install -d -m 0700 "${side}"
echo "==> moving current state aside: ${side}"
if [[ -d "${DATADIR}" ]]; then
    cp -a "${DATADIR}/." "${side}/" || true
fi

stage="$(mktemp -d)"
trap 'rm -rf "${stage}"' EXIT
echo "==> extracting bundle"
tar -xzf "${BUNDLE}" -C "${stage}"
[[ -f "${stage}/backup-manifest.txt" ]] && cat "${stage}/backup-manifest.txt"

echo "==> restoring into ${DATADIR}"
install -d -o helen -g helen -m 0750 "${DATADIR}"
for f in commclient.db server_roles.json access_codes.json \
         control_plane_audit.ndjson node_id.txt secret_master_code.txt; do
    if [[ -f "${stage}/${f}" ]]; then
        install -o helen -g helen -m 0640 "${stage}/${f}" "${DATADIR}/${f}"
        echo "   + ${f}"
    fi
done
if [[ -f "${stage}/server.env" ]]; then
    install -o root -g root -m 0640 "${stage}/server.env" "${ETCDIR}/server.env"
    echo "   + server.env"
fi

chown -R helen:helen "${DATADIR}"

echo "==> starting ${SVC}"
systemctl start "${SVC}"
sleep 3
systemctl is-active --quiet "${SVC}" && echo "  ✓ service active" || {
    echo "  ✗ service failed to come up — rolling back"
    systemctl stop "${SVC}"
    rm -rf "${DATADIR}"; install -d -o helen -g helen -m 0750 "${DATADIR}"
    cp -a "${side}/." "${DATADIR}/"
    systemctl start "${SVC}"
    exit 3
}

echo
echo "savepoint kept at: ${side}  (delete once you've verified restore)"
