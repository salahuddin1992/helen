#!/usr/bin/env bash
# Smoke-test the freshly-built Linux Helen-Server inside WSL.
set -e
SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export JWT_SECRET="$SECRET"
export HELEN_DISABLE_BROADCAST=1
BIN=/mnt/c/Users/youse/c/wifi/CommClient-Server/dist-linux/Helen-Server/Helen-Server
cd "$(dirname "$BIN")"
"$BIN" > /tmp/lin-helen.log 2>&1 &
PID=$!
# Wait until the uvicorn "Application startup complete" line appears
for i in $(seq 1 30); do
    sleep 1
    grep -q "Uvicorn running on\|Application startup complete\|application_startup" /tmp/lin-helen.log 2>/dev/null && break
done
echo "--- key startup events ---"
grep -E 'crash_reporter|audit_chain|calendar_reminder|server_starting' /tmp/lin-helen.log | head -5
echo
echo "--- /api/health ---"
curl -sf http://127.0.0.1:3000/api/health || echo "no response"
echo
echo
echo "--- /api/calendar/events should be 403 (auth-gated) ---"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:3000/api/calendar/events
echo "--- /api/admin/audit-chain/head should be 403 ---"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:3000/api/admin/audit-chain/head
kill $PID 2>/dev/null || true
wait 2>/dev/null || true
echo DONE
