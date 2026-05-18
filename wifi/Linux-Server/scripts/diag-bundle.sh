#!/usr/bin/env bash
# Collect a diagnostic bundle for support / debugging.
# Output: helen-diag-YYYYMMDD-HHMMSS.tar.gz in $PWD (or path given as $1).
# Redacts: master codes, tokens, passwords.
set -euo pipefail

OUT="${1:-$PWD/helen-diag-$(date +%Y%m%d-%H%M%S).tar.gz}"
STAGE="$(mktemp -d -t helen-diag-XXXXXX)"
trap 'rm -rf "${STAGE}"' EXIT

cd "${STAGE}"
mkdir -p system systemd config logs state network

echo "==> system info"
{
    echo "# uname -a";            uname -a
    echo "# /etc/os-release";     cat /etc/os-release 2>/dev/null || true
    echo "# uptime";               uptime
    echo "# cpu";                  lscpu 2>/dev/null | head -20 || true
    echo "# mem";                  free -h
    echo "# disk";                 df -hT /var /opt / 2>/dev/null
    echo "# tuning";               sysctl net.core.rmem_max net.core.wmem_max fs.file-max 2>/dev/null
} > system/info.txt

echo "==> systemd"
systemctl status helen-server --no-pager -l 2>/dev/null > systemd/helen-server.status || true
systemctl show helen-server --no-pager 2>/dev/null > systemd/helen-server.show || true

echo "==> journald (last 10k lines)"
journalctl -u helen-server -n 10000 --no-pager 2>/dev/null > logs/helen-server.journal || true
journalctl -u helen-admin-headless -n 2000 --no-pager 2>/dev/null > logs/helen-admin.journal || true

echo "==> app logs"
if [[ -d /var/log/helen ]]; then
    cp -r /var/log/helen logs/app 2>/dev/null || true
fi

echo "==> config (redacted)"
if [[ -f /etc/helen/server.env ]]; then
    sed -E 's/(SECRET|TOKEN|PASSWORD|KEY)=.*/\1=<redacted>/gI' \
        /etc/helen/server.env > config/server.env 2>/dev/null || true
fi
if [[ -f /etc/helen/admin.env ]]; then
    cp /etc/helen/admin.env config/admin.env
fi

echo "==> state (safe subset — NEVER the master code)"
if [[ -d /var/lib/helen ]]; then
    for f in server_roles.json node_id.txt control_plane_audit.ndjson \
             access_codes.json control_plane_state.json; do
        src="/var/lib/helen/${f}"
        if [[ -f "${src}" ]]; then
            if [[ "${f}" = "access_codes.json" ]]; then
                # Truncate code values to first 4 chars for privacy
                python3 -c "
import json
d = json.load(open('${src}'))
for k,v in d.get('codes', {}).items():
    v['code'] = v['code'][:4] + '****'
json.dump(d, open('state/${f}', 'w'), indent=2)
" 2>/dev/null || cp "${src}" "state/${f}"
            else
                cp "${src}" "state/${f}" 2>/dev/null || true
            fi
        fi
    done
    # DB metadata only (not contents)
    if [[ -f /var/lib/helen/commclient.db ]]; then
        python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/helen/commclient.db')
cur = c.cursor()
cur.execute(\"SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name\")
with open('state/db_schema.txt','w') as f:
    for name, sql in cur.fetchall():
        f.write(f'-- {name}\n{sql or \"\"}\n\n')
cur.execute(\"SELECT name, (SELECT COUNT(*) FROM pragma_table_info(name)) AS cols FROM sqlite_master WHERE type='table'\")
with open('state/db_row_counts.txt','w') as f:
    for name, _ in cur.fetchall():
        try:
            cur.execute(f'SELECT COUNT(*) FROM \"{name}\"')
            f.write(f'{name}: {cur.fetchone()[0]}\n')
        except Exception as e:
            f.write(f'{name}: ? ({e})\n')
" 2>/dev/null || true
    fi
fi

echo "==> network"
{
    echo "# ip addr";            ip -4 addr 2>/dev/null
    echo "# ss listening";       ss -lntu 2>/dev/null | head -40
    echo "# default route";      ip route 2>/dev/null
    echo "# firewall";
    firewall-cmd --list-all 2>/dev/null ||
    ufw status verbose 2>/dev/null    ||
    iptables -L -n 2>/dev/null | head -30 ||
    echo "(no firewall tool detected)"
} > network/net.txt

echo "==> control plane live snapshot"
if curl -fsS --max-time 3 http://127.0.0.1:3000/api/discovery > state/discovery.json 2>/dev/null; then
    true
fi

echo "==> packaging"
cd ..
tar -czf "${OUT}" -C "${STAGE}" .
echo
echo "bundle: ${OUT}"
ls -lh "${OUT}"
echo
echo "review with: tar -tzf ${OUT} | head"
