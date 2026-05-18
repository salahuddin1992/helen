"""Unit tests for Group 2 modules: WAN port-forward manager, TURN
health checker, recursive DNS."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import struct
import sys
from pathlib import Path

import pytest


def _load_router_module(name: str):
    """Load a module from Helen-Router's ``app/`` package by file path.

    Helen-Router's modules import each other as ``app.internal_dns`` —
    but ``app`` already resolves to Helen-Server's package in this test
    process. We side-load the file under the canonical
    ``app.<name>`` key in ``sys.modules`` so cross-imports succeed.
    """
    cand = (Path(__file__).resolve().parents[2]
            / "Helen-Router" / "app" / f"{name}.py")
    canonical = f"app.{name}"
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, str(cand))
    assert spec and spec.loader, f"could not load {cand}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = mod
    spec.loader.exec_module(mod)
    return mod


# ── WAN port-forward manager ──────────────────────────────────────


def test_render_manual_instructions_mikrotik():
    from app.services.wan_port_forward import render_manual_instructions
    out = render_manual_instructions(
        "Mikrotik", external_port=3000, internal_port=3001,
        internal_ip="10.0.0.50", protocol="TCP",
    )
    assert any("dst-nat" in line for line in out)
    assert any("10.0.0.50" in line for line in out)
    assert any("3000" in line for line in out)
    assert any("3001" in line for line in out)


def test_render_manual_instructions_unknown_falls_back_to_generic():
    from app.services.wan_port_forward import render_manual_instructions
    out = render_manual_instructions(
        "TotallyUnknownVendor", external_port=80,
        internal_port=80, internal_ip="192.168.1.5",
    )
    # Generic template references "Port Forwarding"
    assert any("Port Forwarding" in line for line in out)


def test_probe_back_locally_against_open_port():
    from app.services.wan_port_forward import probe_back_locally

    # Open a TCP listener; probe should succeed.
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        result = probe_back_locally("127.0.0.1", port, "TCP")
        assert result["reachable"] is True
        assert result["error"] is None
        assert result["latency_ms"] >= 0
    finally:
        srv.close()


def test_probe_back_locally_against_closed_port():
    from app.services.wan_port_forward import probe_back_locally
    # Pick a port likely closed.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    result = probe_back_locally("127.0.0.1", port, "TCP", timeout_s=1.0)
    assert result["reachable"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_wan_manager_start_stop_no_upnp():
    """Manager should start cleanly with no UPnP URL and report
    sensible status (manual instructions populated, upnp_ok=False)."""
    from app.services.wan_port_forward import (
        configure_wan_portmap, shutdown_wan_portmap,
    )
    mgr = configure_wan_portmap(
        upnp_url=None, external_port=3000, internal_port=3000,
        internal_ip="192.168.1.10", vendor_hint="OpenWrt",
        refresh_interval_s=60,
    )
    await mgr.start()
    try:
        snap = mgr.status()
        assert snap["enabled"] is True
        assert snap["external_port"] == 3000
        assert snap["internal_ip"] == "192.168.1.10"
        assert snap["upnp_ok"] is False
        assert any("LuCI" in s for s in snap["manual_instructions"])
    finally:
        await shutdown_wan_portmap()


# ── TURN health (STUN packet construction) ─────────────────────────


def test_stun_message_round_trip_attrs():
    """Make sure our STUN attribute encoder + parser are inverses."""
    from app.services.turn_health import _attr, _parse_attrs
    a = _attr(0x0006, b"helen-user")
    b = _attr(0x000D, struct.pack("!I", 600))
    parsed = _parse_attrs(a + b)
    assert parsed[0x0006] == b"helen-user"
    assert struct.unpack("!I", parsed[0x000D])[0] == 600


def test_stun_xor_address_decode():
    """Exercise the XOR-MAPPED-ADDRESS decoder against a hand-built
    attribute. RFC 5389 §15.2 example: 192.0.2.1:32853 → xor-encoded."""
    from app.services.turn_health import _decode_xor_address
    # Build a XOR-MAPPED-ADDRESS for 192.0.2.1:32853
    txid = b"\x00" * 12
    family = 0x01
    magic = 0x2112A442
    port = 32853
    ip = socket.inet_aton("192.0.2.1")
    xport = port ^ (magic >> 16)
    xip = struct.unpack("!I", ip)[0] ^ magic
    raw = bytes([0, family]) + struct.pack("!H", xport) + struct.pack("!I", xip)
    decoded = _decode_xor_address(raw, txid)
    assert decoded == ("192.0.2.1", 32853)


def test_short_term_credentials_generation():
    from app.services.turn_health import (
        _make_short_term_username, _make_short_term_password,
    )
    secret = "deadbeef" * 4
    user = _make_short_term_username("alice", ttl_s=300)
    assert user.endswith(":alice")
    assert int(user.split(":")[0]) > 0
    pw = _make_short_term_password(secret, user)
    # base64-encoded SHA1 is 28 chars including the trailing '='
    assert len(pw) == 28
    # Determinism: same inputs → same password
    assert pw == _make_short_term_password(secret, user)


@pytest.mark.asyncio
async def test_stun_binding_against_local_responder():
    """Spin up a tiny in-process STUN responder and verify
    ``stun_binding`` parses our reflexive address out of it."""
    from app.services.turn_health import stun_binding

    # Bind our own UDP socket; write a minimal binding-response when
    # we get a binding-request.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    async def responder():
        loop = asyncio.get_running_loop()
        sock.setblocking(False)
        data, addr = await loop.sock_recvfrom(sock, 1024)
        # data: 20-byte header. Echo back as response with XOR-MAPPED.
        txid = data[8:20]
        magic = 0x2112A442
        xport = addr[1] ^ (magic >> 16)
        xip = struct.unpack("!I", socket.inet_aton(addr[0]))[0] ^ magic
        attr_val = bytes([0, 1]) + struct.pack("!H", xport) + struct.pack(
            "!I", xip,
        )
        attr = struct.pack("!HH", 0x0020, len(attr_val)) + attr_val
        body = attr
        hdr = struct.pack("!HH", 0x0101, len(body)) + struct.pack(
            "!I", magic,
        ) + txid
        await loop.sock_sendto(sock, hdr + body, addr)

    responder_task = asyncio.create_task(responder())
    try:
        result = await stun_binding("127.0.0.1", port=port, timeout_s=2.0)
        assert result.ok is True
        assert result.reflexive_ip == "127.0.0.1"
        assert isinstance(result.reflexive_port, int)
    finally:
        responder_task.cancel()
        sock.close()


@pytest.mark.asyncio
async def test_stun_binding_fails_when_no_server():
    """No STUN server listening — either the recv times out (POSIX)
    or the OS surfaces an ICMP unreachable as a socket error (Windows).
    Both are valid failure modes for a closed UDP port."""
    from app.services.turn_health import stun_binding
    # Pick an unused UDP port.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    result = await stun_binding("127.0.0.1", port=port, timeout_s=0.5)
    assert result.ok is False
    assert result.error  # any non-empty failure message is fine


# ── Recursive DNS (Pi-hole-style) ──────────────────────────────────


def test_blocklist_loader_hosts_format(tmp_path: Path):
    # Load internal_dns first because recursive_dns imports from it.
    _load_router_module("internal_dns")
    rd = _load_router_module("recursive_dns")
    load_blocklist = rd.load_blocklist
    f = tmp_path / "blocklist.txt"
    f.write_text(
        "# A comment\n"
        "0.0.0.0 ads.example.com\n"
        "127.0.0.1 tracker.example.org # inline comment\n"
        "plain-domain.example\n"
        "\n"
        "0.0.0.0 sub.ads.evil.test\n",
        encoding="utf-8",
    )
    blocked = load_blocklist(str(f))
    assert "ads.example.com" in blocked
    assert "tracker.example.org" in blocked
    assert "plain-domain.example" in blocked
    assert "sub.ads.evil.test" in blocked


def test_recursive_server_blocks_subdomains():
    _load_router_module("internal_dns")
    rd = _load_router_module("recursive_dns")
    Zone = sys.modules["app.internal_dns"].Zone
    srv = rd.RecursiveDNSServer(
        Zone(apex="helen.lan"),
        blocklist={"ads.example.com"},
    )
    assert srv._is_blocked("ads.example.com") is True
    assert srv._is_blocked("a.b.ads.example.com") is True
    assert srv._is_blocked("safe.example.com") is False
    assert srv._is_blocked("example.com") is False


def test_recursive_server_nxdomain_for_blocked_query():
    """Build a real DNS query for a blocked name and check we get
    NXDOMAIN back without ever touching an upstream."""
    idns = _load_router_module("internal_dns")
    rd = _load_router_module("recursive_dns")
    Zone = idns.Zone
    _encode_name = idns._encode_name
    srv = rd.RecursiveDNSServer(
        Zone(apex="helen.lan"),
        blocklist={"blocked.example"},
        # Empty upstream list — if blocking fails we'd SERVFAIL.
        upstreams=[],
    )
    # txid=0x1234, flags=0x0100 (RD), 1 question
    header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    question = _encode_name("blocked.example") + struct.pack("!HH", 1, 1)
    query = header + question
    reply = srv._handle_inner(query, ("127.0.0.1", 12345))
    assert reply is not None
    flags = struct.unpack("!H", reply[2:4])[0]
    assert (flags & 0x0F) == 3  # RCODE=NXDOMAIN
    assert srv.stats.blocked == 1


def test_dns_cache_eviction_and_ttl():
    _load_router_module("internal_dns")
    rd = _load_router_module("recursive_dns")
    c = rd._DNSCache(max_entries=2)
    c.put(("a.com", 1), b"reply-a", ttl=60)
    c.put(("b.com", 1), b"reply-b", ttl=60)
    c.put(("c.com", 1), b"reply-c", ttl=60)
    # LRU eviction: a.com was the oldest insert.
    assert c.get(("a.com", 1)) is None
    assert c.get(("b.com", 1)) == b"reply-b"
    assert c.get(("c.com", 1)) == b"reply-c"


def test_dns_stats_top_domains():
    _load_router_module("internal_dns")
    rd = _load_router_module("recursive_dns")
    s = rd.DNSStats()
    for _ in range(5):
        s.record_query("noisy.example")
    s.record_query("quiet.example")
    s.blocked = 2
    s.cache_hits = 3
    out = s.to_dict()
    assert out["queries"] == 6
    assert out["blocked"] == 2
    assert out["top_domains"][0]["name"] == "noisy.example"
    assert out["top_domains"][0]["count"] == 5


def test_recursive_dns_singleton_round_trip():
    from app.core.recursive_dns_singleton import (
        set_recursive_dns, get_recursive_dns, clear_recursive_dns,
    )
    sentinel = object()
    set_recursive_dns(sentinel)
    assert get_recursive_dns() is sentinel
    clear_recursive_dns()
    assert get_recursive_dns() is None
