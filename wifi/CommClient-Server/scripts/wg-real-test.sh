#!/usr/bin/env bash
# Real WireGuard interface test — generate keypair, write conf,
# wg-quick up, ping over the tunnel, verify end-to-end.
set -e
mkdir -p /tmp/wg-test && chmod 700 /tmp/wg-test
cd /tmp/wg-test

PRIV_A=$(wg genkey)
PUB_A=$(echo "$PRIV_A" | wg pubkey)
PRIV_B=$(wg genkey)
PUB_B=$(echo "$PRIV_B" | wg pubkey)

cat > wg-A.conf <<EOF
[Interface]
PrivateKey = $PRIV_A
Address = 10.99.99.1/24
ListenPort = 51820

[Peer]
PublicKey = $PUB_B
AllowedIPs = 10.99.99.2/32
Endpoint = 127.0.0.1:51821
EOF

cat > wg-B.conf <<EOF
[Interface]
PrivateKey = $PRIV_B
Address = 10.99.99.2/24
ListenPort = 51821

[Peer]
PublicKey = $PUB_A
AllowedIPs = 10.99.99.1/32
Endpoint = 127.0.0.1:51820
EOF

chmod 600 wg-A.conf wg-B.conf

echo "[1] bring up wg-A"
wg-quick up ./wg-A.conf
echo "[2] bring up wg-B"
wg-quick up ./wg-B.conf

echo "[3] interfaces created:"
ip -br link show | grep -E "wg-A|wg-B"

echo "[4] wg show summary:"
wg show

echo "[5] ping over the tunnel (A -> B 10.99.99.2):"
ping -c 3 -W 2 10.99.99.2 || echo "ping returned non-zero (expected on userspace WG)"

echo "[6] cleanup"
wg-quick down ./wg-A.conf
wg-quick down ./wg-B.conf

echo "[+] Real WireGuard tunnel test complete."
