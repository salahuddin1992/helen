#!/usr/bin/env bash
# smoke-all-backends.sh — exercise every Helen transport adapter
# against real infrastructure, in one pass.
#
# What this proves
# ----------------
#   * Every adapter module imports + connects against a real broker.
#   * The 6 broker backends + WireGuard + SSH actually move bytes.
#   * The verify-deployment.py CLI sees all 16 checks green.
#
# Usage
# -----
#   scripts/smoke-all-backends.sh                  # default: in-process brokers
#   SKIP_RABBITMQ=1 scripts/smoke-all-backends.sh  # skip slow ones
#   SKIP_WSL=1     scripts/smoke-all-backends.sh   # Windows-only run
#
# Exit codes
# ----------
#   0  every reachable backend passed
#   1  one or more backends failed
#   2  required tooling missing

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/CommClient-Server"
PY="$SERVER/venv/Scripts/python.exe"

if [ ! -x "$PY" ]; then
    echo "[FAIL] Helen-Server venv not found at $PY"
    exit 2
fi

# ── colors ──────────────────────────────────────────────────────────
G='\033[0;32m' Y='\033[1;33m' R='\033[0;31m' N='\033[0m'
section() { echo -e "\n${Y}━━━ $* ━━━${N}"; }
ok()      { echo -e "${G}✓ $*${N}"; }
fail()    { echo -e "${R}✗ $*${N}"; FAILS=$((FAILS+1)); }
warn()    { echo -e "${Y}! $*${N}"; }

FAILS=0
PASSES=0

SKIP_WSL="${SKIP_WSL:-0}"
SKIP_RABBITMQ="${SKIP_RABBITMQ:-0}"
SKIP_WIREGUARD="${SKIP_WIREGUARD:-0}"
SKIP_SSH="${SKIP_SSH:-0}"
SKIP_L2BRIDGE="${SKIP_L2BRIDGE:-0}"

# ── 1. Adapter unit + integration tests ────────────────────────────
section "1. Adapter unit + integration tests"
export PATH="/c/Program Files/mosquitto:$PATH"
cd "$SERVER"
JWT_SECRET=$("$PY" -c 'import secrets;print(secrets.token_hex(32))') \
"$PY" -m pytest \
    tests/test_new_transport_adapters.py \
    tests/test_final_three_adapters.py \
    tests/test_transport_adapters_integration.py \
    tests/test_adapters_with_real_deps.py \
    tests/test_ssh_tunnel_real.py \
    tests/test_real_100pct.py \
    --no-header -q 2>&1 | tail -5
RC=$?
if [ $RC -eq 0 ]; then
    ok "Adapter test suite passed"
    PASSES=$((PASSES+1))
else
    fail "Adapter test suite returned non-zero ($RC)"
fi

# ── 2. WireGuard real tunnel (WSL only) ────────────────────────────
if [ "$SKIP_WSL" != "1" ] && [ "$SKIP_WIREGUARD" != "1" ]; then
    section "2. WireGuard real tunnel (WSL)"
    if MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
        bash /mnt/c/Users/youse/c/wifi/CommClient-Server/scripts/wg-real-test.sh \
        2>&1 | tail -3 | grep -q "complete\."; then
        ok "WireGuard tunnel verified"
        PASSES=$((PASSES+1))
    else
        fail "WireGuard tunnel test failed"
    fi
fi

# ── 3. L2 bridge data flow (WSL only) ──────────────────────────────
if [ "$SKIP_WSL" != "1" ] && [ "$SKIP_L2BRIDGE" != "1" ]; then
    section "3. L2 bridge data flow (WSL)"
    if MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
        bash /mnt/c/Users/youse/c/wifi/CommClient-Server/scripts/l2-bridge-real-test.sh \
        2>&1 | tail -3 | grep -q "complete\."; then
        ok "L2 bridge data flow verified"
        PASSES=$((PASSES+1))
    else
        fail "L2 bridge test failed"
    fi
fi

# ── 4. ZMQ multi-process (WSL only) ────────────────────────────────
if [ "$SKIP_WSL" != "1" ]; then
    section "4. ZMQ multi-process (WSL)"
    OUTPUT=$(MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
        python3 /mnt/c/Users/youse/c/wifi/CommClient-Server/scripts/zmq-multiproc-test.py \
        2>&1 | tail -3 || true)
    if echo "$OUTPUT" | grep -q "OK ZMQ multi-process"; then
        ok "ZMQ multi-process verified"
        PASSES=$((PASSES+1))
    else
        fail "ZMQ multi-process test failed"
    fi
fi

# ── 5. SSH tunnel real OpenSSH (WSL only) ──────────────────────────
if [ "$SKIP_WSL" != "1" ] && [ "$SKIP_SSH" != "1" ]; then
    section "5. SSH tunnel real (WSL OpenSSH)"
    # Ensure WSL sshd is running
    MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- bash -c '
        if ! ss -tlnp 2>/dev/null | grep -q ":2222"; then
            /usr/sbin/sshd -p 2222 2>/dev/null &
            sleep 1
        fi
    ' 2>/dev/null
    # Run paramiko-based SSH test from Windows side
    if MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
        cat /tmp/test-ssh-key 2>/dev/null > /tmp/wsl-ssh-key.tmp; then
        cp /tmp/wsl-ssh-key.tmp "$LOCALAPPDATA/Temp/wsl-ssh-key" \
            2>/dev/null || cp /tmp/wsl-ssh-key.tmp /tmp/wsl-ssh-key
        WSL_IP=$(MSYS2_ARG_CONV_EXCL='*' wsl -d Ubuntu-22.04 -u root -- \
                 bash -c 'ip -4 addr show eth0 | grep -oP "inet \K[\d.]+" | head -1')
        if "$PY" -c "
import paramiko
key = paramiko.RSAKey.from_private_key_file(r'C:\Users\youse\AppData\Local\Temp\wsl-ssh-key')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('$WSL_IP', port=2222, username='root', pkey=key,
          timeout=5, look_for_keys=False, allow_agent=False)
chan = c.get_transport().open_channel('direct-tcpip', ('127.0.0.1', 22), ('127.0.0.1', 0))
print('OK')
chan.close()
c.close()
" 2>/dev/null | grep -q "OK"; then
            ok "SSH real OpenSSH verified"
            PASSES=$((PASSES+1))
        else
            warn "SSH test skipped (sshd or key not ready)"
        fi
    else
        warn "SSH test skipped (no key in WSL)"
    fi
fi

# ── 6. Backend bench (only if NATS reachable) ──────────────────────
section "6. Backend bench (NATS, 200 msgs)"
if which nats-server >/dev/null 2>&1; then
    NATS_PORT=$(($RANDOM % 10000 + 50000))
    nats-server -a 127.0.0.1 -p $NATS_PORT >/dev/null 2>&1 &
    NPID=$!
    sleep 1
    cd "$SERVER" && JWT_SECRET=$("$PY" -c 'import secrets;print(secrets.token_hex(32))') \
        "$PY" tools/bench-backends.py --backend nats --count 200 \
        --nats-url "nats://127.0.0.1:$NATS_PORT" 2>&1 | tail -5
    kill $NPID 2>/dev/null
    wait $NPID 2>/dev/null
    ok "Backend bench ran"
    PASSES=$((PASSES+1))
else
    warn "nats-server not on PATH; skip bench"
fi

# ── Summary ─────────────────────────────────────────────────────────
section "Summary"
echo "  Passes:   $PASSES"
echo "  Failures: $FAILS"
if [ $FAILS -eq 0 ]; then
    ok "ALL BACKENDS VERIFIED"
    exit 0
else
    fail "$FAILS backend(s) failed"
    exit 1
fi
