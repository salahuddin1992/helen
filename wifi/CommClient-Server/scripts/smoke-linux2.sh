#!/usr/bin/env bash
set -e
pkill -f Helen-Server 2>/dev/null || true
sleep 2
cd /mnt/c/Users/youse/c/wifi/CommClient-Server/dist-linux/Helen-Server
export JWT_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export HELEN_DISABLE_BROADCAST=1
./Helen-Server > /tmp/lin2.log 2>&1 &
PID=$!
sleep 20

echo "--- startup events ---"
grep -E "lan_push|orchestrators_wired|reminder|crash_reporter|chain_configured" /tmp/lin2.log | head -5
echo
echo "--- endpoints ---"
curl -s -o /dev/null -w "/api/health: %{http_code}\n" http://127.0.0.1:3000/api/health
curl -s -o /dev/null -w "/api/transcripts/health: %{http_code}\n" http://127.0.0.1:3000/api/transcripts/health
curl -s -o /dev/null -w "/api/calendar/events: %{http_code}\n" http://127.0.0.1:3000/api/calendar/events
curl -s -o /dev/null -w "/api/admin/audit-chain/head: %{http_code}\n" http://127.0.0.1:3000/api/admin/audit-chain/head

kill $PID 2>/dev/null || true
sleep 1
pkill -f Helen-Server 2>/dev/null || true
echo DONE
