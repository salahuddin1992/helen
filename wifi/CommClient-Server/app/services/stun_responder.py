"""
Self-hosted STUN binding responder.

Why a separate module
---------------------
``app.services.turn_health`` is a STUN *client* — it asks an external
STUN server "what's my reflexive address?". That covers the case
where Helen needs to discover its own NAT mapping.

This module is the symmetric counterpart: a STUN *server* baked
into Helen-Server itself. With it running on UDP 3478 (or any port
the operator picks), every LAN client (browsers, mobile WebRTC
stacks, custom dialers) gets a working STUN endpoint without ever
talking to ``stun.l.google.com``. That keeps the deployment
genuinely 100% LAN-only even when WebRTC's ICE machinery insists on
"a STUN server".

What it implements
------------------
RFC 5389 §6 — Binding Request → Binding Response with the source
endpoint reported as ``XOR-MAPPED-ADDRESS``. That's the only message
type WebRTC needs from STUN. We deliberately do **not** implement:

  * TURN (use ``bundled_turn`` + ``turn_health`` for that)
  * STUN long-term credentials
  * STUN over TCP/TLS (UDP-only — covers >99% of WebRTC traffic)

Wire shape
----------
Listens on a single UDP socket. Each datagram is parsed in a few
microseconds; replies are written back to the same source address.
No per-client state — STUN binding is idempotent. Safe to expose
on every interface; refusing non-RFC1918 sources is the operator's
firewall job.

Wired into the lifespan via ``HELEN_STUN_LISTEN`` env var (e.g.
``0.0.0.0:3478``); off by default so the existing bundled coturn
path is unaffected.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_STUN_MAGIC = 0x2112A442
_STUN_BIND_REQUEST = 0x0001
_STUN_BIND_RESPONSE = 0x0101
_STUN_BIND_ERROR = 0x0111
_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_ATTR_SOFTWARE = 0x8022
_ATTR_FINGERPRINT = 0x8028


def _pad4(n: int) -> int:
    return (4 - (n % 4)) % 4


def _attr(typ: int, value: bytes) -> bytes:
    return (struct.pack("!HH", typ, len(value)) + value
            + (b"\x00" * _pad4(len(value))))


def _xor_mapped_address(addr: tuple[str, int],
                         txid: bytes) -> bytes:
    """Encode an XOR-MAPPED-ADDRESS attribute value."""
    ip_str, port = addr
    family = 0x01  # IPv4 — IPv6 is rarely needed for LAN STUN
    xport = port ^ (_STUN_MAGIC >> 16)
    xip = (struct.unpack("!I", socket.inet_aton(ip_str))[0]
           ^ _STUN_MAGIC)
    return (bytes([0, family]) + struct.pack("!H", xport)
            + struct.pack("!I", xip))


def build_binding_response(req: bytes, src_addr: tuple[str, int]) -> bytes:
    """Construct a binding-success response for ``req`` originating at
    ``src_addr``. Returns the full reply bytes ready for sendto()."""
    if len(req) < 20:
        return b""
    txid = req[8:20]
    xa = _xor_mapped_address(src_addr, txid)
    body = (
        _attr(_ATTR_XOR_MAPPED_ADDRESS, xa)
        + _attr(_ATTR_SOFTWARE, b"Helen STUN")
    )
    hdr = (
        struct.pack("!HH", _STUN_BIND_RESPONSE, len(body))
        + struct.pack("!I", _STUN_MAGIC) + txid
    )
    return hdr + body


def build_binding_error(req: bytes, code: int = 400) -> bytes:
    if len(req) < 20:
        return b""
    txid = req[8:20]
    # ERROR-CODE: class (high byte), number (low byte), reason
    class_byte = code // 100
    number_byte = code % 100
    reason = b"Bad Request"
    err_attr_val = (b"\x00\x00" + bytes([class_byte, number_byte])
                    + reason)
    body = _attr(0x0009, err_attr_val)
    hdr = (
        struct.pack("!HH", _STUN_BIND_ERROR, len(body))
        + struct.pack("!I", _STUN_MAGIC) + txid
    )
    return hdr + body


# ── Stats ──────────────────────────────────────────────────────────


@dataclass
class STUNResponderStats:
    started_at: Optional[float] = None
    requests_total: int = 0
    responses_total: int = 0
    parse_errors: int = 0
    last_client: Optional[str] = None
    by_client_count: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        # Top-10 noisiest clients only — bounded payload.
        top = sorted(self.by_client_count.items(),
                      key=lambda kv: kv[1], reverse=True)[:10]
        return {
            "started_at": self.started_at,
            "requests_total": self.requests_total,
            "responses_total": self.responses_total,
            "parse_errors": self.parse_errors,
            "last_client": self.last_client,
            "top_clients": [{"client": c, "count": n} for c, n in top],
        }


# ── Server ─────────────────────────────────────────────────────────


class _STUNProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "STUNResponder") -> None:
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.server._handle(data, addr, self.transport)


class STUNResponder:
    def __init__(self, bind_host: str = "0.0.0.0",
                  bind_port: int = 3478) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.stats = STUNResponderStats()

    async def start(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _STUNProtocol(self),
            local_addr=(self.bind_host, self.bind_port),
            allow_broadcast=False,
        )
        self.stats.started_at = time.time()
        logger.info("stun_responder_started",
                    host=self.bind_host, port=self.bind_port)

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.stats.started_at = None

    def _handle(self, data: bytes, addr: tuple[str, int],
                 transport: Optional[asyncio.DatagramTransport]) -> None:
        if transport is None:
            return
        if len(data) < 20:
            self.stats.parse_errors += 1
            return
        try:
            method, _length = struct.unpack("!HH", data[:4])
            magic = struct.unpack("!I", data[4:8])[0]
        except struct.error:
            self.stats.parse_errors += 1
            return

        # Request must be a binding-class message with the magic cookie.
        if magic != _STUN_MAGIC:
            self.stats.parse_errors += 1
            return
        if method != _STUN_BIND_REQUEST:
            transport.sendto(build_binding_error(data, 400), addr)
            return

        self.stats.requests_total += 1
        client_key = f"{addr[0]}"
        self.stats.last_client = f"{addr[0]}:{addr[1]}"
        self.stats.by_client_count[client_key] = (
            self.stats.by_client_count.get(client_key, 0) + 1
        )

        reply = build_binding_response(data, addr)
        if reply:
            transport.sendto(reply, addr)
            self.stats.responses_total += 1


# ── Singleton helpers ──────────────────────────────────────────────


_responder: Optional[STUNResponder] = None


def configure_stun_responder(bind_host: str = "0.0.0.0",
                              bind_port: int = 3478) -> STUNResponder:
    global _responder
    _responder = STUNResponder(bind_host, bind_port)
    return _responder


def get_stun_responder() -> Optional[STUNResponder]:
    return _responder


async def shutdown_stun_responder() -> None:
    global _responder
    if _responder is not None:
        await _responder.stop()
        _responder = None


def configure_from_env() -> Optional[STUNResponder]:
    """Build a responder from ``HELEN_STUN_LISTEN`` env var (format
    ``host:port``). Returns None if the env var isn't set, so the
    feature is strictly opt-in."""
    raw = os.environ.get("HELEN_STUN_LISTEN", "").strip()
    if not raw:
        return None
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        return configure_stun_responder(host or "0.0.0.0", int(port))
    return configure_stun_responder("0.0.0.0", int(raw))


__all__ = [
    "STUNResponder",
    "STUNResponderStats",
    "build_binding_response",
    "build_binding_error",
    "configure_stun_responder",
    "get_stun_responder",
    "shutdown_stun_responder",
    "configure_from_env",
]
