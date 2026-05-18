#!/usr/bin/env bash
# Helen-Admin installer — Linux.
#
# Usage:  sudo ./install.sh [--binary <path>] [--enable-service] [--no-desktop]
set -euo pipefail

HELEN_USER="${HELEN_USER:-helen}"
ADMIN_USER="${ADMIN_USER:-helen-admin}"
PREFIX="${PREFIX:-/opt/helen/admin}"
ETCDIR="${ETCDIR:-/etc/helen}"
DATADIR="${DATADIR:-/var/lib/helen-admin}"
LOGDIR="${LOGDIR:-/var/log/helen-admin}"

BINARY_SRC=""
ENABLE_SVC=0
INSTALL_DESKTOP=1

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(cd "${here}/.." && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --binary)         BINARY_SRC="$2"; shift 2;;
        --enable-service) ENABLE_SVC=1; shift;;
        --no-desktop)     INSTALL_DESKTOP=0; shift;;
        --help|-h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) echo "unknown flag: $1" >&2; exit 2;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2
    exit 1
fi

echo "==> ensuring service user ${ADMIN_USER} for headless mode"
if ! id -u "${ADMIN_USER}" &>/dev/null; then
    useradd --system --home-dir "${DATADIR}" --shell /usr/sbin/nologin \
            --user-group "${ADMIN_USER}"
fi

echo "==> creating directories"
install -d -o "${ADMIN_USER}" -g "${ADMIN_USER}" -m 0750 "${PREFIX}"
install -d -o root            -g root            -m 0755 "${ETCDIR}"
install -d -o "${ADMIN_USER}" -g "${ADMIN_USER}" -m 0750 "${DATADIR}"
install -d -o "${ADMIN_USER}" -g "${ADMIN_USER}" -m 0755 "${LOGDIR}"

echo "==> locating binary"
if [[ -z "${BINARY_SRC}" ]]; then
    for candidate in \
        "${REPO_ROOT}/CommClient-Server/dist/Helen-Admin" \
        "${REPO_ROOT}/dist/Helen-Admin"; do
        if [[ -d "${candidate}" && -x "${candidate}/Helen-Admin" ]]; then
            BINARY_SRC="${candidate}"
            break
        fi
    done
fi
if [[ -z "${BINARY_SRC}" ]]; then
    echo "no Helen-Admin binary found — run scripts/build.sh first" >&2
    exit 3
fi

echo "==> copying ${BINARY_SRC} → ${PREFIX}"
cp -a "${BINARY_SRC}/." "${PREFIX}/"
chown -R "${ADMIN_USER}:${ADMIN_USER}" "${PREFIX}"
chmod 0755 "${PREFIX}/Helen-Admin"

echo "==> installing config (admin.env)"
if [[ ! -f "${ETCDIR}/admin.env" ]]; then
    install -o root -g root -m 0644 "${here}/scripts/admin.env" "${ETCDIR}/admin.env"
fi

echo "==> installing systemd unit (headless service)"
install -o root -g root -m 0644 \
    "${here}/systemd/helen-admin-headless.service" \
    /etc/systemd/system/helen-admin-headless.service

echo "==> installing helen-admin-ctl CLI"
install -o root -g root -m 0755 \
    "${here}/bin/helen-admin-ctl" /usr/bin/helen-admin-ctl

echo "==> installing launchers"
cat > /usr/bin/helen-admin <<'EOF'
#!/bin/sh
# GUI launcher — for the interactive operator session
set -a
[ -f /etc/helen/admin.env ] && . /etc/helen/admin.env
set +a

EXTRA=""
[ "${HELEN_ADMIN_REMOTE:-0}" = "1" ] && EXTRA="${EXTRA} --remote"
[ "${HELEN_ADMIN_EXPOSE_ON_LAN:-0}" = "1" ] && EXTRA="${EXTRA} --expose-on-lan"

exec /opt/helen/admin/Helen-Admin ${EXTRA} "$@"
EOF
chmod 0755 /usr/bin/helen-admin

if [[ ${INSTALL_DESKTOP} -eq 1 ]]; then
    echo "==> installing .desktop entry"
    install -o root -g root -m 0644 \
        "${here}/desktop/helen-admin.desktop" \
        /usr/share/applications/helen-admin.desktop
    # Update icon cache if icon file present
    if [[ -f "${here}/desktop/helen-admin.svg" ]]; then
        install -o root -g root -m 0644 \
            "${here}/desktop/helen-admin.svg" \
            /usr/share/icons/hicolor/scalable/apps/helen-admin.svg
        gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
    fi
fi

systemctl daemon-reload

if [[ ${ENABLE_SVC} -eq 1 ]]; then
    echo "==> enabling + starting helen-admin-headless.service"
    systemctl enable --now helen-admin-headless.service
    sleep 2
    systemctl status helen-admin-headless.service --no-pager -l | head -15 || true
else
    echo "==> headless service installed but not enabled"
    echo "    start it with:  sudo systemctl enable --now helen-admin-headless"
fi

cat <<EOF

┌─────────────────────────────────────────────────────┐
│  Helen-Admin installed                              │
│                                                     │
│  GUI:       helen-admin                             │
│  headless:  sudo systemctl start helen-admin-headless │
│  browser:   http://127.0.0.1:5173  (headless mode)  │
│  config:    /etc/helen/admin.env                    │
└─────────────────────────────────────────────────────┘
EOF
