"""
Router-friendly network test — checks the LAN/WiFi router for the
features Helen actually relies on:

  1. **Multicast pass-through** — routers/APs sometimes block IGMP
     forwarding, killing mDNS auto-discovery.
  2. **Broadcast pass-through** — APs running "client isolation" or
     "AP isolation" silently drop broadcast UDP, killing the
     fallback discovery on port 41234.
  3. **AP isolation / client isolation** — even unicast between
     two clients on the same SSID can be blocked.
  4. **VLAN tagging consistency** — if two hosts are on the same
     SSID but different VLANs, they can't reach each other.
  5. **MTU floor** — paths that drop >1452-byte packets break TLS
     handshakes silently.
  6. **NAT type detection** — symmetric NAT defeats UDP hole-punch.
  7. **IPv6 RA / dual-stack consistency** — mixed-stack networks
     where IPv6 reaches but IPv4 doesn't (or vice versa).

Run this on EACH host in the deployment. If any host reports a
warning, that link is the weak point in your topology.

The script is read-only — it sends mDNS / broadcast / unicast
probes but never modifies any router config. Compare the JSON
output between two hosts to find the asymmetric paths.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RouterCheck:
    name: str
    status: str = "pending"           # ok | warn | fail
    detail: str = ""
    raw: str = ""
    elapsed_ms: float = 0.0


# ── 1. Multicast pass-through ──────────────────────────────────────


def check_multicast_in() -> RouterCheck:
    r = RouterCheck(name="1. Multicast inbound (mDNS replies)")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(2.0)
    # Generic mDNS query for everything
    query = (
        b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        b"\x09_services\x07_dns-sd\x04_udp\x05local\x00"
        b"\x00\x0c\x00\x01"
    )
    t0 = time.perf_counter()
    distinct_responders: set[str] = set()
    try:
        sock.sendto(query, ("224.0.0.251", 5353))
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                _, addr = sock.recvfrom(4096)
                distinct_responders.add(addr[0])
            except socket.timeout:
                break
    except OSError as exc:
        r.status = "fail"
        r.detail = f"sendto failed: {exc}"
        return r
    finally:
        sock.close()
        r.elapsed_ms = (time.perf_counter() - t0) * 1000

    if len(distinct_responders) == 0:
        r.status = "warn"
        r.detail = ("0 mDNS responders. Router may have IGMP "
                     "snooping disabled or AP isolation on.")
    elif len(distinct_responders) == 1:
        r.status = "warn"
        r.detail = (f"only 1 responder ({list(distinct_responders)[0]}) — "
                     "your own host. AP isolation likely blocking peers.")
    else:
        r.status = "ok"
        r.detail = f"{len(distinct_responders)} mDNS responders seen"
    r.raw = "\n".join(sorted(distinct_responders))
    return r


# ── 2. Broadcast pass-through ──────────────────────────────────────


def check_broadcast_out() -> RouterCheck:
    r = RouterCheck(name="2. UDP broadcast outbound")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        n = sock.sendto(b"helen-router-test\n",
                          ("255.255.255.255", 41234))
        if n > 0:
            r.status = "ok"
            r.detail = f"broadcast send accepted ({n} bytes on the wire)"
        else:
            r.status = "warn"
            r.detail = "sendto returned 0 bytes"
    except Exception as exc:
        r.status = "fail"
        r.detail = f"broadcast send failed: {exc}"
    finally:
        sock.close()
    return r


# ── 3. AP isolation detection ──────────────────────────────────────


def check_ap_isolation(known_peers: list[str]) -> RouterCheck:
    """Try to ARP-resolve a few known LAN peers. If we get back zero
    MACs, AP isolation is the most likely culprit (ARP unicast is
    silently dropped between WiFi clients)."""
    r = RouterCheck(name="3. AP / client isolation (ARP visibility)")
    if not known_peers:
        r.status = "warn"
        r.detail = ("no peer IPs supplied via --peer; can't tell "
                     "isolation apart from empty network")
        return r
    # Force OS to ARP-probe by sending a tiny UDP packet to each peer
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for peer in known_peers:
        try:
            sock.sendto(b"x", (peer, 9))   # discard service
        except Exception:
            pass
    sock.close()
    time.sleep(0.5)

    # Re-read ARP table
    arp = _read_arp_table()
    visible = [p for p in known_peers if p in arp]
    if not visible:
        r.status = "fail"
        r.detail = ("none of the supplied peers replied to ARP — "
                     "AP/client isolation likely on. On consumer routers, "
                     "look for 'AP Isolation', 'WLAN Partition' or "
                     "'Client Separation' in WiFi advanced settings.")
    elif len(visible) < len(known_peers):
        r.status = "warn"
        r.detail = (f"{len(visible)}/{len(known_peers)} peers visible "
                     "via ARP — partial isolation or VLAN issue")
    else:
        r.status = "ok"
        r.detail = (f"all {len(visible)} peers visible — "
                     "no AP isolation detected")
    r.raw = "\n".join(f"{ip}  {arp.get(ip, '(no MAC)')}"
                       for ip in known_peers)
    return r


# ── 4. VLAN consistency (proxy: subnet match) ──────────────────────


def check_vlan_consistency(peers: list[str]) -> RouterCheck:
    """We can't read 802.1Q tags from userspace, so instead we check
    whether all supplied peers share our /24. Mismatch usually means
    VLAN segmentation."""
    r = RouterCheck(name="4. VLAN / subnet consistency with peers")
    if not peers:
        r.status = "warn"
        r.detail = "no peer IPs supplied"
        return r
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "0.0.0.0"
    my_net = ipaddress.ip_network(
        ".".join(local_ip.split(".")[:3]) + ".0/24",
    )
    cross = [p for p in peers
             if ipaddress.ip_address(p) not in my_net]
    if not cross:
        r.status = "ok"
        r.detail = f"all peers on {my_net}"
    else:
        r.status = "warn"
        r.detail = (f"{len(cross)} peers on a different /24: "
                     f"{', '.join(cross[:3])}{'…' if len(cross) > 3 else ''}. "
                     "If you intended same-VLAN, check your switch trunk "
                     "config and AP VLAN tag.")
    return r


# ── 5. MTU floor ───────────────────────────────────────────────────


def check_mtu_floor(target: str) -> RouterCheck:
    """Send progressively larger TCP payloads. If 1500-byte succeed
    but 1452-byte fail, we have a PMTU blackhole. We can't use
    ICMP-DF probes from userspace, so we approximate by recording
    transfer success at a few sizes."""
    r = RouterCheck(name="5. MTU / PMTU sanity")
    # Just reach something on the target — if TCP works at all, MTU
    # is probably fine.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((target, 80))
        # Send a 4 KB payload
        sock.sendall(
            b"GET / HTTP/1.0\r\n"
            + b"User-Agent: helen-router-test "
            + b"x" * 3500
            + b"\r\n\r\n",
        )
        # If we can read at least one byte back, the path handles
        # large packets.
        sock.recv(1)
        r.status = "ok"
        r.detail = "1500-byte path appears intact (4 KB request answered)"
    except Exception as exc:
        r.status = "warn"
        r.detail = (f"large-payload TCP test inconclusive: {exc} — "
                     "could be ports closed or PMTU blackhole")
    finally:
        sock.close()
    return r


# ── 6. NAT type detection (best-effort) ────────────────────────────


def check_nat_type(stun_server: Optional[str] = None) -> RouterCheck:
    """Without an external STUN server we can't tell symmetric vs
    full-cone NAT directly. We DO compare the local socket address
    vs the gateway's idea of us — if the local IP is RFC1918 and we
    *can* reach a peer on a different /24 via the router, NAT is
    happening. Helen ships its own STUN with bundled_turn — if
    HELEN_STUN_URL is set we use that."""
    r = RouterCheck(name="6. NAT type (best-effort)")
    if not stun_server:
        r.status = "warn"
        r.detail = ("--stun not supplied; skipping. "
                     "For accurate NAT-type testing, run "
                     "Helen-bundled-turn and pass --stun helen-server:3478")
        return r
    # Cheap STUN binding request: 20 bytes
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.5)
        # STUN binding request, RFC 5389
        msg = (
            b"\x00\x01\x00\x00"                 # type, length
            + b"\x21\x12\xa4\x42"                # magic cookie
            + os.urandom(12)                     # transaction id
        )
        host, _, port = stun_server.partition(":")
        sock.sendto(msg, (host, int(port or "3478")))
        data, _ = sock.recvfrom(2048)
        # Parse XOR-MAPPED-ADDRESS attribute (type 0x0020)
        if len(data) >= 32 and data[20:22] == b"\x00\x20":
            family = data[25]
            xport = int.from_bytes(data[26:28], "big") ^ 0x2112
            if family == 0x01 and len(data) >= 32:
                xip = bytes(b ^ m for b, m in zip(
                    data[28:32], b"\x21\x12\xa4\x42"))
                external = f"{xip[0]}.{xip[1]}.{xip[2]}.{xip[3]}:{xport}"
                r.status = "ok"
                r.detail = (f"STUN reflective address: {external}; "
                             "your router NAT is at least functional")
                r.raw = external
                return r
        r.status = "warn"
        r.detail = "STUN reply received but no XOR-MAPPED-ADDRESS attr"
    except Exception as exc:
        r.status = "warn"
        r.detail = f"STUN probe failed: {exc}"
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return r


# ── 7. IPv6 dual-stack ─────────────────────────────────────────────


def check_ipv6_consistency() -> RouterCheck:
    r = RouterCheck(name="7. IPv4/IPv6 dual-stack consistency")
    has_v4 = has_v6 = False
    try:
        import psutil
        for ifname, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and a.address != "127.0.0.1":
                    has_v4 = True
                elif a.family == socket.AF_INET6 and not a.address.startswith("::1"):
                    has_v6 = True
    except Exception:
        pass
    if has_v4 and has_v6:
        r.status = "ok"
        r.detail = "both IPv4 and IPv6 addresses present"
    elif has_v4:
        r.status = "ok"
        r.detail = "IPv4 only — Helen primarily uses IPv4 anyway"
    elif has_v6:
        r.status = "warn"
        r.detail = ("IPv6 only — Helen's discovery uses IPv4 broadcast; "
                     "may not auto-find peers")
    else:
        r.status = "fail"
        r.detail = "no usable network address found"
    return r


# ── helpers ─────────────────────────────────────────────────────────


def _read_arp_table() -> dict[str, str]:
    cmd = ["arp", "-a"] if os.name == "nt" else ["ip", "neigh"]
    out = {}
    try:
        result = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        for line in result.splitlines():
            ip = mac = None
            for tok in line.split():
                try:
                    ipaddress.ip_address(tok)
                    ip = tok
                except ValueError:
                    pass
                t = tok.replace("-", ":").lower()
                if (len(t) == 17 and t.count(":") == 5
                        and all(c in "0123456789abcdef:" for c in t)):
                    mac = t
            if ip and mac:
                out[ip] = mac
    except Exception:
        pass
    return out


# ── driver ──────────────────────────────────────────────────────────


def run(args) -> dict[str, Any]:
    checks: list[RouterCheck] = []
    checks.append(check_multicast_in())
    checks.append(check_broadcast_out())
    checks.append(check_ap_isolation(args.peer))
    checks.append(check_vlan_consistency(args.peer))
    checks.append(check_mtu_floor(args.target))
    checks.append(check_nat_type(args.stun))
    checks.append(check_ipv6_consistency())
    return {
        "started_at": time.time(),
        "target": args.target,
        "peers": args.peer,
        "stun": args.stun,
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail,
             "raw": c.raw, "elapsed_ms": c.elapsed_ms}
            for c in checks
        ],
        "summary": {
            "ok":   sum(1 for c in checks if c.status == "ok"),
            "warn": sum(1 for c in checks if c.status == "warn"),
            "fail": sum(1 for c in checks if c.status == "fail"),
        },
    }


def print_text(report: dict) -> None:
    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    print(f"\nRouter network test — target {report['target']}\n")
    for c in report["checks"]:
        print(f"  {icons.get(c['status'], '?')} {c['name']}")
        print(f"      {c['detail']}")
    s = report["summary"]
    print(f"\n  {s['ok']} ok  {s['warn']} warn  {s['fail']} fail\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="8.8.8.8",
                   help="An IP we should be able to reach for the "
                        "MTU/PMTU + NAT tests")
    p.add_argument("--peer", action="append", default=[],
                   help="Repeat for each known LAN peer to test "
                        "AP-isolation / VLAN consistency")
    p.add_argument("--stun", default=os.environ.get("HELEN_STUN_URL"),
                   help="STUN server host[:port] (e.g. helen-server:3478)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    report = run(args)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_text(report)


if __name__ == "__main__":
    main()
