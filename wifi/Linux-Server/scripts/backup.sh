#!/usr/bin/env bash
# Atomic backup of Helen-Server state.
#
# Produces a single tar.gz containing:
#   commclient.db      (SQLite: backed up via .backup so it's consistent)
#   server_roles.json
#   access_codes.json
#   control_plane_audit.ndjson
#   secret_master_code.txt   (sensitive — only if --include-secret)
#   server.env               (if --include-config)
#
# Usage:
#   sudo backup.sh                              -> /var/backups/helen/YYYY-MM-DD.tar.gz
#   sudo backup.sh /path/to/dir                  (custom destination)
#   sudo backup.sh --include-secret --include-config
set -euo pipefail

DATADIR="${DATADIR:-/var/lib/helen}"
ETCDIR="${ETCDIR:-/etc/helen}"
DEST_DIR="${DEST_DIR:-/var/backups/helen}"
INCLUDE_SECRET=0
INCLUDE_CONFIG=0
EXPLICIT_DEST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --include-secret) INCLUDE_SECRET=1; shift;;
        --include-config) INCLUDE_CONFIG=1; shift;;
        --help|-h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) EXPLICIT_DEST="$1"; shift;;
    esac
done

[[ -n "${EXPLICIT_DEST}" ]] && DEST_DIR="${EXPLICIT_DEST%/*}"

if [[ $EUID -ne 0 ]]; then
    echo "must run as root" >&2; exit 1
fi

install -d -m 0700 "${DEST_DIR}"
stage="$(mktemp -d -t helen-bak-XXXXXX)"
trap 'rm -rf "${stage}"' EXIT

echo "==> snapshotting DB"
if [[ -f "${DATADIR}/commclient.db" ]]; then
    sqlite3 "${DATADIR}/commclient.db" ".backup ${stage}/commclient.db"
fi

echo "==> copying state files"
for f in server_roles.json access_codes.json control_plane_audit.ndjson node_id.txt; do
    [[ -f "${DATADIR}/${f}" ]] && cp "${DATADIR}/${f}" "${stage}/${f}"
done

if [[ ${INCLUDE_SECRET} -eq 1 && -f "${DATADIR}/secret_master_code.txt" ]]; then
    cp "${DATADIR}/secret_master_code.txt" "${stage}/secret_master_code.txt"
    chmod 0600 "${stage}/secret_master_code.txt"
fi

if [[ ${INCLUDE_CONFIG} -eq 1 && -f "${ETCDIR}/server.env" ]]; then
    cp "${ETCDIR}/server.env" "${stage}/server.env"
fi

# Record metadata
cat > "${stage}/backup-manifest.txt" <<EOF
helen-server backup
created_at: $(date -Iseconds)
hostname:   $(hostname)
datadir:    ${DATADIR}
include_secret: ${INCLUDE_SECRET}
include_config: ${INCLUDE_CONFIG}
files:
$(cd "${stage}" && ls -l | tail -n +2)
EOF

out="${EXPLICIT_DEST:-${DEST_DIR}/helen-$(date +%Y-%m-%d_%H%M%S).tar.gz}"
echo "==> packaging → ${out}"
tar -czf "${out}" -C "${stage}" .
chmod 0600 "${out}"

ls -lh "${out}"
echo
echo "restore with:  sudo helenctl restore ${out}"
