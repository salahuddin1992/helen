"""Minimal STUN client (RFC 5389).

Sends a Binding Request to a STUN server and parses the
XOR-MAPPED-ADDRESS attribute from the response. Used by
``nat_detector`` to discover our public reflexive address.

This is a hand-rolled minimal implementation — no external dependency.
For production-grade STUN/TURN, swap to ``aiortc.contrib.signaling`` or
``stun-py``. The interface here stays stable either way.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from typing import Optional

from app.core.logging import get_logger
from app.nat.nat_exceptions import STUNError

logger = get_logger(__name__)


# STUN message types (RFC 5389 §6).
STUN_BINDING_REQUEST  = 0x0001
STUN_BINDING_RESPONSE = 0x0101
STUN_MAGIC_COOKIE     = 0x2112A442

# Attribute types.
ATTR_MAPPED_ADDRESS     = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020


def _build_request() -> tuple[bytes, bytes]:
    """Return (packet, transaction_id_bytes)."""
    txid = os.urandom(12)
    msg_type = STUN_BINDING_REQUEST
    msg_len  = 0
    header = struct.pack(
        "!HHI", msg_type, msg_len, STUN_MAGIC_COOKIE,
    ) + txid
    return header, txid


def _parse_xor_mapped(data: bytes, txid: bytes) -> tuple[str, int]:
    """Walk attributes, return (host, port) from XOR-MAPPED-ADDRESS."""
    if len(data) < 20:
        raise STUNError("response too short")
    msg_type, msg_len, magic = struct.unpack("!HHI", data[:8])
    if msg_type != STUN_BINDING_RESPONSE:
        raise STUNError(f"unexpected message type 0x{msg_type:04x}")
    if magic != STUN_MAGIC_COOKIE:
        raise STUNError("bad magic cookie")
    rx_txid = data[8:20]
    if rx_txid != txid:
        raise STUNError("transaction id mismatch")

    body = data[20:20 + msg_len]
    i = 0
    fallback: Optional[tuple[str, int]] = None
    while i + 4 <= len(body):
        attr_type, attr_len = struct.unpack("!HH", body[i:i + 4])
        attr_val = body[i + 4: i + 4 + attr_len]
        if attr_type == ATTR_XOR_MAPPED_ADDRESS:
            if len(attr_val) < 8:
                raise STUNError("XOR-MAPPED too short")
            family = attr_val[1]
            if family != 0x01:  # IPv4 only
                raise STUNError(f"unsupported family 0x{family:02x}")
            xport, = struct.unpack("!H", attr_val[2:4])
            port = xport ^ (STUN_MAGIC_COOKIE >> 16)
            xaddr = attr_val[4:8]
            ip_int = struct.unpack("!I", xaddr)[0] ^ STUN_MAGIC_COOKIE
            host = socket.inet_ntoa(struct.pack("!I", ip_int))
            return host, port
        if attr_type == ATTR_MAPPED_ADDRESS and len(attr_val) >= 8:
            family = attr_val[1]
            if family == 0x01:
                port, = struct.unpack("!H", attr_val[2:4])
                host = socket.inet_ntoa(attr_val[4:8])
                fallback = (host, port)
        # Pad attribute to 4-byte boundary.
        i += 4 + attr_len + ((4 - (attr_len % 4)) % 4)

    if fallback:
        return fallback
    raise STUNError("no MAPPED-ADDRESS attribute in response")


async def query(host: str, port: int = 3478,
                *, timeout: float = 3.0) -> tuple[str, int]:
    """Send a STUN Binding Request to ``(host, port)``; return the
    public-reflexive (ip, port) of the local socket as seen by the
    server. Raises STUNError on any failure.
    """
    if not host:
        raise STUNError("STUN host required")
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    try:
        request, txid = _build_request()
        await loop.sock_connect(sock, (host, int(port)))
        await loop.sock_sendall(sock, request)
        try:
            data = await asyncio.wait_for(
                loop.sock_recv(sock, 1024), timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise STUNError(f"timeout from {host}:{port}")
        return _parse_xor_mapped(data, txid)
    except STUNError:
        raise
    except Exception as e:
        raise STUNError(f"transport: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
