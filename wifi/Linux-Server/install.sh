#!/usr/bin/env bash
# Helen-Server installer for Linux.
# Idempotent: safe to re-run to upgrade binaries.
#
# Usage:  sudo ./install.sh
#         sudo ./install.sh --binary /path/to/Helen-Server   (custom source)
#         sudo ./install.sh --no-enable                       (install but don't start)
set -euo pipefail

HELEN_USER="${HELEN_USER:-helen}"
HELEN_GROUP="${HELEN_GROUP:-helen}"
PREFIX="${PREFIX:-/opt/helen/server}"
ETCDIR="${ETCDIR:-/etc/helen}"
DATADIR="${DATADIR:-/var/lib/helen}"
LOGDIR="${LOGDIR:-/var/log/helen}"
RUNDIR="${RUNDIR:-/run/helen}"

SERVICE_FILE="/etc/systemd/system/helen-server.service"
ENV_FILE="${ETCDIR}/server.env"
BINARY_SRC=""
DO_ENABLE=1

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(cd "${here}/.." && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --binary)    BINARY_SRC="$2"; shift 2;;
        --no-enable) DO_ENABLE=0; shift;;
        --help|-h)
            grep '^#' "$0" | grep -v '!/' | sed 's/^# \{0,1\}//'
            exit 0;;
        *) echo "unknown flag: $1" >&2; exit 2;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2
    exit 1
fi

# Run pre-flight checks (non-blocking: warn but continue)
if [[ -x "${here}/scripts/preflight.sh" ]]; then
    echo "==> pre-flight checks"
    "${here}/scripts/preflight.sh" || {
        rc=$?
        if [[ $rc -eq 1 ]]; then
            echo "pre-flight FAILED — aborting. Fix issues or pass --skip-preflight" >&2
            echo "(pass --skip-preflight to override, not recommended)" >&2
            for a in "$@"; do [[ "$a" = "--skip-preflight" ]] && break 2; done
            exit 10
        fi
        echo "pre-flight returned warnings — continuing"
    }
fi

echo "==> creating system user: ${HELEN_USER}"
if ! id -u "${HELEN_USER}" &>/dev/null; then
    useradd --system --home-dir "${DATADIR}" --shell /usr/sbin/nologin \
            --user-group "${HELEN_USER}"
fi

echo "==> creating directories"
install -d -o "${HELEN_USER}" -g "${HELEN_GROUP}" -m 0750 "${PREFIX}"
install -d -o root            -g root            -m 0755 "${ETCDIR}"
install -d -o "${HELEN_USER}" -g "${HELEN_GROUP}" -m 0750 "${DATADIR}"
install -d -o "${HELEN_USER}" -g "${HELEN_GROUP}" -m 0755 "${LOGDIR}"
install -d -o "${HELEN_USER}" -g "${HELEN_GROUP}" -m 0755 "${RUNDIR}"

echo "==> locating binary"
if [[ -z "${BINARY_SRC}" ]]; then
    # Preferred: PyInstaller --onedir output.
    for candidate in \
        "${REPO_ROOT}/CommClient-Server/dist/Helen-Server" \
        "${REPO_ROOT}/dist/Helen-Server"; do
        if [[ -d "${candidate}" && -x "${candidate}/Helen-Server" ]]; then
            BINARY_SRC="${candidate}"
            break
        fi
    done
fi
if [[ -z "${BINARY_SRC}" ]]; then
    echo "no binary found — run scripts/build.sh first, or pass --binary <path>" >&2
    exit 3
fi

echo "==> copying ${BINARY_SRC} → ${PREFIX}"
# rsync is nicer but not always present; cp -a is universal
cp -a "${BINARY_SRC}/." "${PREFIX}/"
chown -R "${HELEN_USER}:${HELEN_GROUP}" "${PREFIX}"
chmod 0755 "${PREFIX}/Helen-Server"

echo "==> installing config (${ENV_FILE})"
if [[ ! -f "${ENV_FILE}" ]]; then
    install -o root -g root -m 0640 "${here}/config/helen-server.env" "${ENV_FILE}"
    echo "    wrote default env — review ${ENV_FILE} before starting"
else
    echo "    existing ${ENV_FILE} preserved"
fi

echo "==> installing logrotate"
install -o root -g root -m 0644 \
    "${here}/config/logrotate.conf" /etc/logrotate.d/helen-server

echo "==> installing systemd unit"
install -o root -g root -m 0644 \
    "${here}/systemd/helen-server.service" "${SERVICE_FILE}"

echo "==> installing /usr/bin/helen-server launcher"
cat > /usr/bin/helen-server <<'EOF'
#!/bin/sh
# Launcher — honors /etc/helen/server.env
set -a
[ -f /etc/helen/server.env ] && . /etc/helen/server.env
set +a
exec /opt/helen/server/Helen-Server "$@"
EOF
chmod 0755 /usr/bin/helen-server

echo "==> installing helenctl + scripts"
install -o root -g root -m 0755 "${here}/bin/helenctl" /usr/bin/helenctl
for s in preflight.sh diag-bundle.sh backup.sh restore.sh healthcheck.sh; do
    install -o root -g root -m 0755 "${here}/scripts/${s}" \
        "${PREFIX}/scripts/${s}"
done
# Ensure scripts dir exists (may not be present in --onedir output)
install -d -o "${HELEN_USER}" -g "${HELEN_GROUP}" -m 0755 "${PREFIX}/scripts"

echo "==> installing nightly backup timer"
install -o root -g root -m 0644 \
    "${here}/systemd/helen-server-backup.service" \
    /etc/systemd/system/helen-server-backup.service
install -o root -g root -m 0644 \
    "${here}/systemd/helen-server-backup.timer" \
    /etc/systemd/system/helen-server-backup.timer
install -d -o root -g root -m 0700 /var/backups/helen

echo "==> installing shell completion + manpage"
install -o root -g root -m 0644 \
    "${here}/completion/helenctl.bash" \
    /etc/bash_completion.d/helenctl 2>/dev/null || true
install -o root -g root -m 0644 \
    "${here}/man/helenctl.1" /usr/share/man/man1/helenctl.1 2>/dev/null || true
command -v mandb >/dev/null 2>&1 && mandb -q 2>/dev/null || true

echo "==> installing sysctl tuning"
install -o root -g root -m 0644 \
    "${here}/config/sysctl-helen.conf" /etc/sysctl.d/99-helen.conf
sysctl --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-helen.conf >/dev/null 2>&1 || true

# AppArmor profile — install but DON'T enforce by default
# (needs review against the operator's distro). Operator runs aa-enforce
# when they've tested.
if command -v apparmor_parser >/dev/null 2>&1 && [[ -d /etc/apparmor.d ]]; then
    echo "==> installing AppArmor profile (complain mode)"
    install -o root -g root -m 0644 \
        "${here}/apparmor/usr.local.helen-server" \
        /etc/apparmor.d/opt.helen.server.Helen-Server
    apparmor_parser -r /etc/apparmor.d/opt.helen.server.Helen-Server 2>/dev/null \
        && aa-complain /opt/helen/server/Helen-Server 2>/dev/null \
        || true
fi

echo "==> reloading systemd"
systemctl daemon-reload
systemctl enable --now helen-server-backup.timer 2>/dev/null || true

if [[ ${DO_ENABLE} -eq 1 ]]; then
    echo "==> enabling + starting helen-server.service"
    systemctl enable --now helen-server.service
    sleep 2
    systemctl status helen-server.service --no-pager -l | head -20 || true
else
    echo "==> install complete (service not enabled; pass --no-enable omitted to enable)"
fi

if [[ ${DO_ENABLE} -eq 1 ]]; then
    echo "==> post-install smoke test"
    sleep 2
    ok=0
    for i in 1 2 3 4 5 6 7 8; do
        code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 \
                http://127.0.0.1:3000/api/health 2>/dev/null || echo 000)"
        if [[ "${code}" = "200" ]]; then
            ok=1; echo "   ✓ health probe OK after ${i}s"; break
        fi
        sleep 1
    done
    if [[ ${ok} -eq 0 ]]; then
        echo "   ⚠ health probe did not return 200 in 8s" >&2
        echo "     run:  sudo journalctl -u helen-server -n 100" >&2
    fi
fi

cat <<EOF

┌─────────────────────────────────────────────────┐
│  Helen-Server installed                         │
│                                                 │
│  status:   sudo systemctl status helen-server   │
│  logs:     sudo journalctl -u helen-server -f   │
│  health:   curl http://127.0.0.1:\${PORT:-3000}/api/health  │
│  config:   ${ENV_FILE}                          │
│  data:     ${DATADIR}                           │
└─────────────────────────────────────────────────┘
EOF
