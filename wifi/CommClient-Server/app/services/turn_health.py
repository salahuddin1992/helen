"""
TURN reachability + STUN binding self-test.

What it does
------------
Given a TURN/STUN server (typically the bundled coturn from
``app.services.bundled_turn``), this module:

  1. Sends a STUN binding request (RFC 5389 §6) and parses the
     ``XOR-MAPPED-ADDRESS`` from the response. Confirms the server
     is reachable and reports the *reflexive* address Helen sees
     itself as.

  2. Performs a TURN ``Allocate`` request (RFC 5766 §6) using the
     short-term HMAC credentials Helen mints for itself. Confirms
     the server can actually relay — not just answer pings.

Both probes are pure-Python (struct-level packet handling). No
``aioice`` / ``aiortc`` dependency. Plays nicely inside the
PyInstaller bundle.

Why this matters
----------------
Bundled coturn is configured by ``bundled_turn.ensure_bundled_turn``,
but we never *verify* it works. With this module, the admin endpoint
``/api/admin/transports/turn/health`` says either "binding+allocate
OK, p50=12ms" or "TURN refused: 401 Unauthorized — secret mismatch",
which an operator can act on immediately.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from typing import Optional


# ── STUN message constants ─────────────────────────────────────────


_STUN_MAGIC = 0x2112A442
_STUN_BIND_REQUEST = 0x0001
_STUN_BIND_RESPONSE = 0x0101
_STUN_ALLOCATE_REQUEST = 0x0003
_STUN_ALLOCATE_RESPONSE = 0x0103
_STUN_ALLOCATE_ERROR = 0x0113

_ATTR_MAPPED_ADDRESS = 0x0001
_ATTR_USERNAME = 0x0006
_ATTR_MESSAGE_INTEGRITY = 0x0008
_ATTR_ERROR_CODE = 0x0009
_ATTR_REALM = 0x0014
_ATTR_NONCE = 0x0015
_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_ATTR_REQUESTED_TRANSPORT = 0x0019
_ATTR_LIFETIME = 0x000D
_ATTR_XOR_RELAYED_ADDRESS = 0x0016


def _pad4(n: int) -> int:
    return (4 - (n % 4)) % 4


def _build_message(method: int, txid: bytes, attrs: bytes) -> bytes:
    return struct.pack("!HH", method, len(attrs)) + struct.pack(
        "!I", _STUN_MAGIC,
    ) + txid + attrs


def _attr(typ: int, value: bytes) -> bytes:
    return struct.pack("!HH", typ, len(value)) + value + (b"\x00" * _pad4(len(value)))


def _parse_attrs(body: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    i = 0
    while i + 4 <= len(body):
        typ, length = struct.unpack("!HH", body[i:i + 4])
        i += 4
        out[typ] = body[i:i + length]
        i += length + _pad4(length)
    return out


def _decode_xor_address(value: bytes, txid: bytes) -> Optional[tuple[str, int]]:
    if len(value) < 8:
        return None
    family = value[1]
    xport = struct.unpack("!H", value[2:4])[0] ^ (_STUN_MAGIC >> 16)
    if family == 0x01:  # IPv4
        x = struct.unpack("!I", value[4:8])[0] ^ _STUN_MAGIC
        ip = socket.inet_ntoa(struct.pack("!I", x))
        return ip, xport
    if family == 0x02:  # IPv6
        magic_txid = struct.pack("!I", _STUN_MAGIC) + txid
        raw = bytes(a ^ b for a, b in zip(value[4:20], magic_txid))
        try:
            ip = socket.inet_ntop(socket.AF_INET6, raw)
            return ip, xport
        except OSError:
            return None
    return None


# ── Result types ───────────────────────────────────────────────────


@dataclass
class STUNBindingResult:
    ok: bool
    server: str
    reflexive_ip: Optional[str] = None
    reflexive_port: Optional[int] = None
    rtt_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class TURNAllocateResult:
    ok: bool
    server: str
    relayed_ip: Optional[str] = None
    relayed_port: Optional[int] = None
    lifetime_s: Optional[int] = None
    rtt_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class TURNHealth:
    binding: STUNBindingResult
    allocate: TURNAllocateResult
    healthy_at: float


# ── STUN binding probe ─────────────────────────────────────────────


async def stun_binding(host: str, port: int = 3478,
                        timeout_s: float = 3.0) -> STUNBindingResult:
    """Send a STUN binding request and parse the XOR-MAPPED-ADDRESS.

    Returns the reflexive address the server saw us at — the closest
    we can get to "what's my external IP" without hitting a public
    service."""
    txid = secrets.token_bytes(12)
    msg = _build_message(_STUN_BIND_REQUEST, txid, b"")
    server = f"{host}:{port}"

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        t0 = time.perf_counter()
        await loop.sock_sendto(sock, msg, (host, port))
        try:
            data, _ = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 2048), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return STUNBindingResult(
                ok=False, server=server, error="timeout",
            )
        rtt = (time.perf_counter() - t0) * 1000.0

        if len(data) < 20:
            return STUNBindingResult(
                ok=False, server=server, rtt_ms=rtt,
                error="short response",
            )
        method, length = struct.unpack("!HH", data[:4])
        if method != _STUN_BIND_RESPONSE:
            return STUNBindingResult(
                ok=False, server=server, rtt_ms=rtt,
                error=f"bad method 0x{method:04x}",
            )
        attrs = _parse_attrs(data[20:20 + length])
        xa = attrs.get(_ATTR_XOR_MAPPED_ADDRESS) or attrs.get(_ATTR_MAPPED_ADDRESS)
        if xa is None:
            return STUNBindingResult(
                ok=False, server=server, rtt_ms=rtt,
                error="no MAPPED-ADDRESS in response",
            )
        if _ATTR_XOR_MAPPED_ADDRESS in attrs:
            decoded = _decode_xor_address(xa, txid)
        else:
            # Plain MAPPED-ADDRESS layout: 0,family,port,ip
            port_hi = struct.unpack("!H", xa[2:4])[0]
            ip = socket.inet_ntoa(xa[4:8])
            decoded = (ip, port_hi)
        if not decoded:
            return STUNBindingResult(
                ok=False, server=server, rtt_ms=rtt,
                error="MAPPED-ADDRESS unparseable",
            )
        ip, prt = decoded
        return STUNBindingResult(
            ok=True, server=server,
            reflexive_ip=ip, reflexive_port=prt, rtt_ms=rtt,
        )
    except OSError as exc:
        return STUNBindingResult(
            ok=False, server=server, error=f"socket: {exc}",
        )
    finally:
        sock.close()


# ── TURN allocate probe (with HMAC short-term creds) ───────────────


def _make_short_term_username(user_id: str = "helen-health",
                                ttl_s: int = 60) -> str:
    expiry = int(time.time()) + ttl_s
    return f"{expiry}:{user_id}"


def _make_short_term_password(secret: str, username: str) -> str:
    digest = hmac.new(secret.encode("utf-8"),
                       username.encode("utf-8"),
                       hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _md5_key(username: str, realm: str, password: str) -> bytes:
    return hashlib.md5(
        f"{username}:{realm}:{password}".encode("utf-8"),
    ).digest()


def _add_message_integrity(msg_so_far: bytes, key: bytes) -> bytes:
    """Re-pack ``msg_so_far`` with a MESSAGE-INTEGRITY attribute.

    The HMAC covers the message header (with adjusted length) plus
    every attribute up to but not including the integrity attribute
    itself. RFC 5389 §15.4."""
    # Adjust length in header to include the upcoming MI attr (24 bytes).
    new_len = len(msg_so_far) - 20 + 24
    hdr = msg_so_far[:2] + struct.pack("!H", new_len) + msg_so_far[4:20]
    body_for_hmac = hdr + msg_so_far[20:]
    mac = hmac.new(key, body_for_hmac, hashlib.sha1).digest()
    return msg_so_far + _attr(_ATTR_MESSAGE_INTEGRITY, mac)


async def turn_allocate(
    host: str,
    secret: str,
    *,
    port: int = 3478,
    realm: str = "helen.local",
    user_id: str = "helen-health",
    timeout_s: float = 4.0,
) -> TURNAllocateResult:
    """Issue an allocate request against the TURN server. Two-step
    handshake: first request gets a 401 + nonce/realm, second request
    includes MESSAGE-INTEGRITY signed with the long-term credential."""
    server = f"{host}:{port}"
    txid = secrets.token_bytes(12)
    username = _make_short_term_username(user_id)
    password = _make_short_term_password(secret, username)

    # Step 1: bare allocate, expect 401.
    requested_transport = struct.pack("!BBBB", 17, 0, 0, 0)  # UDP=17
    attrs = (
        _attr(_ATTR_REQUESTED_TRANSPORT, requested_transport)
        + _attr(_ATTR_LIFETIME, struct.pack("!I", 600))
    )
    msg = _build_message(_STUN_ALLOCATE_REQUEST, txid, attrs)

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        t0 = time.perf_counter()
        await loop.sock_sendto(sock, msg, (host, port))
        try:
            data, _ = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 2048), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return TURNAllocateResult(
                ok=False, server=server, error="timeout on first allocate",
            )

        method, length = struct.unpack("!HH", data[:4])
        attrs1 = _parse_attrs(data[20:20 + length])
        nonce = attrs1.get(_ATTR_NONCE)
        srv_realm = attrs1.get(_ATTR_REALM, realm.encode("utf-8")).decode(
            "utf-8", "replace",
        )
        if not nonce:
            err = attrs1.get(_ATTR_ERROR_CODE, b"")
            return TURNAllocateResult(
                ok=False, server=server,
                error=f"first allocate had no NONCE: {err!r}",
            )

        # Step 2: same allocate, but add USERNAME+REALM+NONCE+MI.
        txid2 = secrets.token_bytes(12)
        username_b = username.encode("utf-8")
        attrs2_pre = (
            _attr(_ATTR_REQUESTED_TRANSPORT, requested_transport)
            + _attr(_ATTR_LIFETIME, struct.pack("!I", 600))
            + _attr(_ATTR_USERNAME, username_b)
            + _attr(_ATTR_REALM, srv_realm.encode("utf-8"))
            + _attr(_ATTR_NONCE, nonce)
        )
        msg2 = _build_message(_STUN_ALLOCATE_REQUEST, txid2, attrs2_pre)

        # coturn with use-auth-secret accepts the short-term password
        # as the long-term password. Build the MD5 key on that basis.
        key = _md5_key(username, srv_realm, password)
        msg2 = _add_message_integrity(msg2, key)

        await loop.sock_sendto(sock, msg2, (host, port))
        try:
            data2, _ = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 2048), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return TURNAllocateResult(
                ok=False, server=server,
                error="timeout on authenticated allocate",
            )
        rtt = (time.perf_counter() - t0) * 1000.0

        method2, length2 = struct.unpack("!HH", data2[:4])
        if method2 != _STUN_ALLOCATE_RESPONSE:
            attrs_err = _parse_attrs(data2[20:20 + length2])
            err = attrs_err.get(_ATTR_ERROR_CODE, b"")
            return TURNAllocateResult(
                ok=False, server=server, rtt_ms=rtt,
                error=f"allocate refused method=0x{method2:04x} err={err!r}",
            )

        attrs2 = _parse_attrs(data2[20:20 + length2])
        relayed = attrs2.get(_ATTR_XOR_RELAYED_ADDRESS)
        relayed_ip = relayed_port = None
        if relayed:
            decoded = _decode_xor_address(relayed, txid2)
            if decoded:
                relayed_ip, relayed_port = decoded
        lifetime_b = attrs2.get(_ATTR_LIFETIME)
        lifetime = struct.unpack("!I", lifetime_b)[0] if lifetime_b else None
        return TURNAllocateResult(
            ok=True, server=server,
            relayed_ip=relayed_ip, relayed_port=relayed_port,
            lifetime_s=lifetime, rtt_ms=rtt,
        )
    except OSError as exc:
        return TURNAllocateResult(
            ok=False, server=server, error=f"socket: {exc}",
        )
    finally:
        sock.close()


# ── Combined health check ──────────────────────────────────────────


async def check_turn_health(
    host: str,
    *,
    port: int = 3478,
    secret: Optional[str] = None,
    realm: str = "helen.local",
    user_id: str = "helen-health",
) -> TURNHealth:
    """Run STUN binding + TURN allocate in sequence. ``secret``
    defaults to ``HELEN_TURN_SECRET`` env var; if missing or empty,
    the allocate probe is skipped (binding-only result)."""
    binding = await stun_binding(host, port=port)
    secret = secret or os.environ.get("HELEN_TURN_SECRET", "")
    if not secret:
        allocate = TURNAllocateResult(
            ok=False, server=f"{host}:{port}",
            error="no HELEN_TURN_SECRET configured (binding-only check)",
        )
    else:
        allocate = await turn_allocate(
            host, secret, port=port, realm=realm, user_id=user_id,
        )
    return TURNHealth(
        binding=binding, allocate=allocate, healthy_at=time.time(),
    )


def health_to_dict(h: TURNHealth) -> dict:
    return {
        "checked_at": h.healthy_at,
        "binding": {
            "ok": h.binding.ok,
            "server": h.binding.server,
            "reflexive_ip": h.binding.reflexive_ip,
            "reflexive_port": h.binding.reflexive_port,
            "rtt_ms": h.binding.rtt_ms,
            "error": h.binding.error,
        },
        "allocate": {
            "ok": h.allocate.ok,
            "server": h.allocate.server,
            "relayed_ip": h.allocate.relayed_ip,
            "relayed_port": h.allocate.relayed_port,
            "lifetime_s": h.allocate.lifetime_s,
            "rtt_ms": h.allocate.rtt_ms,
            "error": h.allocate.error,
        },
        "healthy": h.binding.ok and (h.allocate.ok or
                                       "no HELEN_TURN_SECRET" in
                                       (h.allocate.error or "")),
    }


__all__ = [
    "STUNBindingResult", "TURNAllocateResult", "TURNHealth",
    "stun_binding", "turn_allocate", "check_turn_health",
    "health_to_dict",
]
