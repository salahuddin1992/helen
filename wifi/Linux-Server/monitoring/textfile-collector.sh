#!/usr/bin/env bash
# Textfile collector for node_exporter.
# Cron this every 15s (systemd timer also works) and point node_exporter
# at /var/lib/node_exporter/textfile_collector/.
#
# Translates Helen's JSON status endpoints into prom-format metrics.
set -euo pipefail

OUT_DIR="${OUT_DIR:-/var/lib/node_exporter/textfile_collector}"
HELEN_BASE="${HELEN_BASE:-http://127.0.0.1:3000}"
TMP="${OUT_DIR}/.helen.prom.$$"
FINAL="${OUT_DIR}/helen.prom"

mkdir -p "${OUT_DIR}"

# Public discovery — always reachable
disc="$(curl -fsS --max-time 2 "${HELEN_BASE}/api/discovery" 2>/dev/null || echo '{}')"
up=0
name=unknown
version=unknown
if [[ "${disc}" != '{}' ]]; then
    up=1
    name="$(echo "${disc}" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("name","?"))' 2>/dev/null)"
    version="$(echo "${disc}" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("version","?"))' 2>/dev/null)"
fi

{
    echo "# HELP helen_up Helen-Server reachability"
    echo "# TYPE helen_up gauge"
    echo "helen_up{name=\"${name}\",version=\"${version}\"} ${up}"

    # Admin-only metrics require HELEN_ADMIN_TOKEN
    if [[ -n "${HELEN_ADMIN_TOKEN:-}" ]]; then
        status="$(curl -fsS --max-time 2 \
            -H "Authorization: Bearer ${HELEN_ADMIN_TOKEN}" \
            "${HELEN_BASE}/api/admin/control-plane/status" 2>/dev/null || echo '{}')"
        if [[ "${status}" != '{}' ]]; then
            python3 <<EOF
import json, sys
s = json.loads('''${status}''')
g = s.get('global', {})
i = s.get('inputs', {})
phases = ['normal','degraded','emergency','frozen']
print("# HELP helen_control_plane_phase Current phase (1=active)")
print("# TYPE helen_control_plane_phase gauge")
for p in phases:
    v = 1 if g.get('phase') == p else 0
    print(f'helen_control_plane_phase{{phase="{p}"}} {v}')
print("# HELP helen_cpu_p95 CPU p95 percentage over last 30s")
print("# TYPE helen_cpu_p95 gauge")
print(f"helen_cpu_p95 {i.get('cpu_p95', 0)}")
print("# HELP helen_rss_p95 Memory p95 percentage")
print("# TYPE helen_rss_p95 gauge")
print(f"helen_rss_p95 {i.get('rss_p95', 0)}")
print("# HELP helen_active_sockets Active Socket.IO connections")
print("# TYPE helen_active_sockets gauge")
print(f"helen_active_sockets {i.get('active_sockets', 0)}")
print("# HELP helen_rooms_total Rooms tracked by control plane")
print("# TYPE helen_rooms_total gauge")
print(f"helen_rooms_total {len(s.get('rooms', []))}")
print("# HELP helen_admission_open 1 if new rooms accepted, 0 if frozen")
print("# TYPE helen_admission_open gauge")
print(f"helen_admission_open {1 if g.get('admission_open') else 0}")
EOF
        fi
    fi
} > "${TMP}"
mv -f "${TMP}" "${FINAL}"
