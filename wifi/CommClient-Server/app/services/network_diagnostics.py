"""
Network diagnostics — pinpoint WHY a client/peer can't reach this server.

Checks every common "router blocks the path" failure mode and reports
one-line conclusions the admin dashboard can render verbatim. Each
check returns (name, ok, detail) so the UI can green-tick the good
ones and surface only the failures.

Checks (in order):

  1. self_bind          — we're listening on 0.0.0.0 (not 127.0.0.1 only)
  2. gateway_ping       — default gateway answers ICMP-like TCP probe
  3. broadcast_send     — UDP 255.255.255.255 send succeeds
  4. firewall_inbound   — our ports 3000/3001/3443 accept from our own LAN IP
  5. mdns_registered    — ``helen.local`` resolves to our IP from localhost
  6. peer_asymmetry     — if we see a peer but they don't see us → AP isolation
  7. multicast_rx       — we've received ANY UDP on 41234 in the last minute
  8. outbound_reach     — we can reach another host on the LAN by IP

Everything is best-effort read-only; no router credentials needed.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


def _default_gateway() -> str | None:
    """Parse `route print -4` for the default gateway."""
    try:
        out = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True, timeout=3, text=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            import re as _re
            if _re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[2]) and not parts[2].startswith("127."):
                return parts[2]
    return None


def _my_lan_ip() -> str | None:
    """Best-effort enumeration of this host's primary LAN IP."""
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except OSError:
        pass
    return None


async def _tcp_probe(host: str, port: int, timeout: float = 2.0) -> tuple[bool, float]:
    """Open a TCP connection, measure RTT. Returns (reachable, ms)."""
    t0 = time.monotonic()
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, (time.monotonic() - t0) * 1000.0
    except (OSError, asyncio.TimeoutError):
        return False, (time.monotonic() - t0) * 1000.0


async def _check_self_bind(port: int) -> dict[str, Any]:
    """Are we on 0.0.0.0 or only 127.0.0.1? The former means LAN clients
    can reach us; the latter means the server is accessible only from
    this machine."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, timeout=3, text=True, check=False,
        )
    except Exception as e:
        return {"name": "self_bind", "ok": False,
                "detail": f"netstat failed: {e}"}
    on_wildcard = False
    on_loopback_only = False
    for line in out.stdout.splitlines():
        if f":{port} " not in line or "LISTENING" not in line:
            continue
        if "0.0.0.0:" + str(port) in line or "[::]:" + str(port) in line:
            on_wildcard = True
        elif "127.0.0.1:" + str(port) in line:
            on_loopback_only = True
    if on_wildcard:
        return {"name": "self_bind", "ok": True,
                "detail": f"listening on 0.0.0.0:{port} (LAN reachable)"}
    if on_loopback_only:
        return {"name": "self_bind", "ok": False,
                "detail": f"bound 127.0.0.1:{port} only — set HOST=0.0.0.0"}
    return {"name": "self_bind", "ok": False,
            "detail": f"no listener on port {port}"}


async def _check_gateway_ping(gw: str | None) -> dict[str, Any]:
    if gw is None:
        return {"name": "gateway_ping", "ok": False,
                "detail": "no default gateway detected"}
    # Gateway usually replies to TCP 53 or 80; try both before giving up.
    for port in (53, 80, 443, 22, 8291):  # 8291 is Mikrotik winbox
        ok, ms = await _tcp_probe(gw, port, timeout=1.0)
        if ok:
            return {"name": "gateway_ping", "ok": True,
                    "detail": f"gateway {gw} reachable on port {port} ({ms:.0f}ms)"}
    return {"name": "gateway_ping", "ok": False,
            "detail": f"gateway {gw} didn't answer any common port — "
                      f"router may block ICMP/closed-port RST"}


async def _check_broadcast_send() -> dict[str, Any]:
    """Send a UDP packet to 255.255.255.255:41234. Success = we can at
    least originate broadcasts (doesn't prove they *reach* peers)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        sock.sendto(b"helen-diag-probe", ("255.255.255.255", 41234))
        sock.close()
        return {"name": "broadcast_send", "ok": True,
                "detail": "UDP broadcast send succeeded"}
    except OSError as e:
        return {"name": "broadcast_send", "ok": False,
                "detail": f"broadcast send failed: {e}"}


async def _check_firewall_inbound(port: int) -> dict[str, Any]:
    """Connect to OUR port via our own LAN IP (not loopback). If this
    fails, Windows Firewall is probably blocking inbound on that port
    even for LAN origins."""
    ip = _my_lan_ip()
    if ip is None:
        return {"name": "firewall_inbound", "ok": False,
                "detail": "couldn't determine LAN IP"}
    ok, ms = await _tcp_probe(ip, port, timeout=2.0)
    if ok:
        return {"name": "firewall_inbound", "ok": True,
                "detail": f"{ip}:{port} reachable via LAN IP ({ms:.0f}ms)"}
    return {"name": "firewall_inbound", "ok": False,
            "detail": f"Windows Firewall may be blocking inbound TCP {port} "
                      f"— run: netsh advfirewall firewall add rule "
                      f"name=Helen dir=in action=allow protocol=TCP "
                      f"localport={port}"}


async def _check_mdns_registered() -> dict[str, Any]:
    """Resolve helen.local locally. Success proves the mDNS responder is
    advertising a hostname the OS can consume."""
    try:
        loop = asyncio.get_event_loop()
        addrs = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo("helen.local", None, socket.AF_INET),
        )
        if addrs:
            ip = addrs[0][4][0]
            return {"name": "mdns_registered", "ok": True,
                    "detail": f"helen.local -> {ip}"}
    except (socket.gaierror, OSError) as e:
        return {"name": "mdns_registered", "ok": False,
                "detail": f"helen.local did not resolve ({e}) — "
                          f"mDNS responder down or OS resolver disabled"}
    return {"name": "mdns_registered", "ok": False,
            "detail": "helen.local did not resolve"}


async def _check_peer_asymmetry() -> dict[str, Any]:
    """AP isolation detection: we see peer X, but X doesn't list us
    in *its* peer table. If discovery is symmetric, every pair is
    mutual. Asymmetry strongly suggests client isolation."""
    try:
        from app.services.peer_registry import peer_registry
        from app.services.discovery_service import get_server_id
        peers = await peer_registry.list(include_stale=False)
    except Exception as e:
        return {"name": "peer_asymmetry", "ok": False,
                "detail": f"peer_registry unavailable: {e}"}
    if not peers:
        return {"name": "peer_asymmetry", "ok": True,
                "detail": "no peers yet — can't test symmetry"}

    import httpx as _httpx
    my_id = get_server_id()
    suspect: list[str] = []
    async with _httpx.AsyncClient(timeout=3.0) as c:
        for p in peers[:5]:   # sample at most 5 peers to keep the probe cheap
            try:
                r = await c.get(f"http://{p.host}:{p.port}/api/peers")
                if r.status_code != 200:
                    continue
                remote_peers = r.json().get("peers") or []
                if not any(rp.get("server_id") == my_id for rp in remote_peers):
                    suspect.append(f"{p.name}({p.host})")
            except Exception:
                continue
    if not suspect:
        return {"name": "peer_asymmetry", "ok": True,
                "detail": f"discovery symmetric with {len(peers)} peers"}
    return {"name": "peer_asymmetry", "ok": False,
            "detail": f"we see {len(suspect)} peer(s) that DO NOT see us: "
                      f"{', '.join(suspect)} — probable AP/client isolation"}


async def _check_multicast_rx() -> dict[str, Any]:
    """We've received a UDP peer broadcast in the last minute? If zero
    peers have ever been discovered via UDP, multicast/broadcast is
    probably being dropped by the router/switch."""
    try:
        from app.services.peer_registry import peer_registry
        peers = await peer_registry.list(include_stale=False)
    except Exception as e:
        return {"name": "multicast_rx", "ok": False,
                "detail": f"peer_registry unavailable: {e}"}
    if not peers:
        return {"name": "multicast_rx", "ok": False,
                "detail": "no UDP peer broadcasts received yet — "
                          "router may be filtering multicast/broadcast"}
    return {"name": "multicast_rx", "ok": True,
            "detail": f"received broadcasts from {len(peers)} peer(s)"}


async def _check_outbound_reach() -> dict[str, Any]:
    """Can we reach ANY known peer? Proves our NIC is functional and the
    router lets us talk outbound at all."""
    try:
        from app.services.peer_registry import peer_registry
        peers = await peer_registry.list(include_stale=False)
    except Exception:
        peers = []
    if not peers:
        return {"name": "outbound_reach", "ok": True,
                "detail": "skipped — no peers to probe yet"}
    peer = peers[0]
    ok, ms = await _tcp_probe(peer.host, peer.port, timeout=2.0)
    if ok:
        return {"name": "outbound_reach", "ok": True,
                "detail": f"reached {peer.name} ({peer.host}:{peer.port}) in {ms:.0f}ms"}
    return {"name": "outbound_reach", "ok": False,
            "detail": f"could not reach {peer.name} ({peer.host}:{peer.port}) — "
                      f"router may block east-west TCP or peer is dead"}


# Maps each failed check to a specific operator action, in order of
# urgency (most blocking first). The dashboard surfaces these so an
# admin sees "what to do" rather than just "what's wrong".
_REMEDIATION = {
    "self_bind": (
        "Server is listening on loopback only — clients on other devices "
        "cannot reach it. Restart the server with HOST=0.0.0.0 (or check "
        "firewall rules that may be redirecting bind to localhost)."
    ),
    "firewall_inbound": (
        "Local firewall (Windows Defender / iptables / pf) is blocking "
        "inbound traffic on the server port. Add an inbound allow rule "
        "for the configured port from your LAN subnet."
    ),
    "broadcast_send": (
        "UDP broadcast is being dropped — peer auto-discovery via UDP "
        "won't work. Switch the LAN to a hub/switch (not a gateway in AP "
        "isolation mode) or rely on mDNS/manual seed."
    ),
    "mdns_registered": (
        "mDNS service is not registered — Bonjour-aware clients (macOS/"
        "iOS) won't find this server by name. Install the zeroconf "
        "package and confirm multicast 224.0.0.251 is allowed by the firewall."
    ),
    "peer_asymmetry": (
        "Peers can see us but we can't see them (or vice versa). Common "
        "cause: AP isolation / client isolation enabled on the WiFi "
        "router. Disable client isolation in the access-point settings."
    ),
    "multicast_rx": (
        "No UDP packets received on the discovery port for >1 minute — "
        "either no peers are broadcasting or multicast is filtered. "
        "Verify another peer is running and check switch IGMP snooping."
    ),
    "gateway_ping": (
        "Default gateway not reachable — the host has lost LAN routing. "
        "Check the network adapter and DHCP lease."
    ),
    "outbound_reach": (
        "Cannot reach other LAN hosts by IP — verify the network adapter "
        "is up and no host firewall is blocking outbound TCP."
    ),
}


async def run_diagnostics(port: int) -> dict[str, Any]:
    """Execute every check, return a single JSON blob the dashboard
    renders as a table of green/red rows."""
    gw = _default_gateway()
    my_ip = _my_lan_ip()
    checks = await asyncio.gather(
        _check_self_bind(port),
        _check_gateway_ping(gw),
        _check_broadcast_send(),
        _check_firewall_inbound(port),
        _check_mdns_registered(),
        _check_peer_asymmetry(),
        _check_multicast_rx(),
        _check_outbound_reach(),
        return_exceptions=True,
    )
    results = []
    for r in checks:
        if isinstance(r, dict):
            results.append(r)
        else:
            results.append({"name": "error", "ok": False,
                            "detail": f"{type(r).__name__}: {r}"})
    failures = sum(1 for r in results if not r.get("ok"))
    recommendations = [
        {"check": r.get("name"), "action": _REMEDIATION[r.get("name")]}
        for r in results
        if not r.get("ok") and r.get("name") in _REMEDIATION
    ]
    return {
        "lan_ip": my_ip,
        "gateway": gw,
        "checks": results,
        "failures": failures,
        "total": len(results),
        "overall_ok": failures == 0,
        "recommendations": recommendations,
    }
