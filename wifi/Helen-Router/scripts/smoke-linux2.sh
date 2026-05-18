#!/usr/bin/env bash
set -e
pkill -9 -f Helen-Router 2>/dev/null || true
sleep 2
export HELEN_ROUTER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export HELEN_ROUTER_DISABLE_MDNS=1
export HELEN_ROUTER_DISABLE_RTT=1
BIN=/mnt/c/Users/youse/c/wifi/Helen-Router/dist-linux/Helen-Router/Helen-Router
"$BIN" > /tmp/r.log 2>&1 &
PID=$!
sleep 8
echo "--- log ---"
grep -E "router_mesh|router_started" /tmp/r.log | head -2
echo
echo "--- /router/health ---"
curl -sf http://127.0.0.1:8080/router/health
echo
echo
echo "--- /mesh/topology ---"
curl -sf http://127.0.0.1:8080/mesh/topology
echo
kill $PID 2>/dev/null || true
sleep 1
pkill -9 -f Helen-Router 2>/dev/null || true
echo DONE
