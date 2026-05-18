#!/usr/bin/env bash
# Smoke-test the Linux Helen-Router build.
set -e
pkill -f Helen-Router 2>/dev/null || true
sleep 2
BIN=/mnt/c/Users/youse/c/wifi/Helen-Router/dist-linux/Helen-Router/Helen-Router
export HELEN_ROUTER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export HELEN_ROUTER_DISABLE_MDNS=1
export HELEN_ROUTER_DISABLE_RTT=1
"$BIN" > /tmp/router.log 2>&1 &
PID=$!
sleep 8

echo "--- mesh startup ---"
grep -E "router_mesh_started|router_started" /tmp/router.log | head -2

echo
echo "--- /router/health ---"
curl -sf http://127.0.0.1:8080/router/health 2>&1 | head -1
echo
echo "--- /mesh/topology ---"
curl -sf http://127.0.0.1:8080/mesh/topology 2>&1 | head -1
echo
echo "--- /mesh/path/unknown should be 404 ---"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8080/mesh/path/unknown

kill $PID 2>/dev/null || true
sleep 1
pkill -f Helen-Router 2>/dev/null || true
echo DONE
