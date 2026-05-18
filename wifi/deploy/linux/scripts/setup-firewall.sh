#!/usr/bin/env bash
# setup-firewall.sh [server|rendezvous|both]
# Configures ufw or firewalld to allow Helen ports from RFC1918 ranges only.
# Detects the active firewall manager automatically.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[ERROR] Run as root: sudo bash $0"
  exit 1
fi

ROLE="${1:-both}"

# Detect firewall manager
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  FW=ufw
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  FW=firewalld
elif command -v iptables >/dev/null; then
  FW=iptables
else
  echo "[!] No supported firewall manager found. Open ports manually:"
  echo "    Server:     TCP 3000, 3443  | UDP 41234, 5353"
  echo "    Rendezvous: TCP 9090, 9101, 9102"
  exit 0
fi

echo "[*] Firewall manager: $FW"

LAN_RANGES=(127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16)

allow_tcp() {
  local port=$1
  local label=$2
  case "$FW" in
    ufw)
      for r in "${LAN_RANGES[@]}"; do
        ufw allow from "$r" to any port "$port" proto tcp comment "$label" >/dev/null
      done
      ;;
    firewalld)
      for r in "${LAN_RANGES[@]}"; do
        firewall-cmd --permanent --zone=internal --add-rich-rule="rule family=ipv4 source address=$r port protocol=tcp port=$port accept" >/dev/null || true
      done
      ;;
    iptables)
      for r in "${LAN_RANGES[@]}"; do
        iptables -C INPUT -p tcp -s "$r" --dport "$port" -j ACCEPT 2>/dev/null || \
        iptables -A INPUT -p tcp -s "$r" --dport "$port" -j ACCEPT
      done
      ;;
  esac
  echo "[+] $label — TCP $port allowed from RFC1918"
}

allow_udp() {
  local port=$1
  local label=$2
  case "$FW" in
    ufw)
      for r in "${LAN_RANGES[@]}"; do
        ufw allow from "$r" to any port "$port" proto udp comment "$label" >/dev/null
      done
      ;;
    firewalld)
      for r in "${LAN_RANGES[@]}"; do
        firewall-cmd --permanent --zone=internal --add-rich-rule="rule family=ipv4 source address=$r port protocol=udp port=$port accept" >/dev/null || true
      done
      ;;
    iptables)
      for r in "${LAN_RANGES[@]}"; do
        iptables -C INPUT -p udp -s "$r" --dport "$port" -j ACCEPT 2>/dev/null || \
        iptables -A INPUT -p udp -s "$r" --dport "$port" -j ACCEPT
      done
      ;;
  esac
  echo "[+] $label — UDP $port allowed from RFC1918"
}

if [ "$ROLE" = "server" ] || [ "$ROLE" = "both" ]; then
  allow_tcp 3000  "Helen-Server HTTP"
  allow_tcp 3443  "Helen-Server HTTPS"
  allow_udp 41234 "Helen-Server discovery"
  allow_udp 5353  "Helen-Server mDNS"
fi

if [ "$ROLE" = "rendezvous" ] || [ "$ROLE" = "both" ]; then
  allow_tcp 9090 "Helen-Rendezvous HTTP"
  allow_tcp 9101 "Helen-Rendezvous Relay backend"
  allow_tcp 9102 "Helen-Rendezvous Relay frontend"
fi

# Reload
case "$FW" in
  firewalld)
    firewall-cmd --reload >/dev/null
    ;;
  iptables)
    if command -v iptables-save >/dev/null && [ -d /etc/iptables ]; then
      iptables-save > /etc/iptables/rules.v4
    fi
    ;;
esac

echo "[✓] Firewall configured for $ROLE"
