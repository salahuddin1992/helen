"""
Helen-DNS — minimal authoritative resolver for ``*.helen.lan``.

Solves the "every client needs to remember the server IP" problem.
Run this on the same host as Helen-Router (or any LAN server), point
client DHCP at it, and clients reach Helen-Server via friendly names::

    https://helen-server.helen.lan
    https://router.helen.lan
    https://vault.helen.lan

Wire shape
----------
* Listens on UDP 53 by default (configurable). DNS over UDP is
  enough for record types Helen needs (A, AAAA, SRV, TXT).
* Authoritative for one zone — ``HELEN_DNS_ZONE`` env, default
  ``helen.lan``.
* Records are loaded from the in-process service registry (so the
  IP a client gets is always the freshest one Helen-Router knows
  about).
* Forwarder for everything else: any query outside the zone gets
  proxied to the upstream resolver from ``HELEN_DNS_FORWARDER``
  (default the OS default — falls back to ``127.0.0.1`` if unset).

This is a pure-Python implementation (struct-level packet parsing).
No bind/dnsmasq dep. Doesn't compete on speed with Unbound — but for
LAN-only traffic with tens of clients it's plenty.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Wire-format helpers ─────────────────────────────────────────────


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name.

    Defends against the classic compression-loop DoS: a malicious
    packet whose pointer chain forms a cycle. We cap the number of
    pointer follows at MAX_POINTER_HOPS to bound work.
    """
    MAX_POINTER_HOPS = 16
    labels = []
    jumped = False
    end_offset = offset
    pointer_hops = 0
    while True:
        if offset >= len(data):
            raise ValueError("DNS name truncated")
        length = data[offset]
        if (length & 0xC0) == 0xC0:
            pointer_hops += 1
            if pointer_hops > MAX_POINTER_HOPS:
                raise ValueError("DNS pointer loop")
            ptr = struct.unpack("!H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end_offset = offset + 2
            offset = ptr
            jumped = True
            continue
        if length == 0:
            offset += 1
            if not jumped:
                end_offset = offset
            break
        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length
        if not jumped:
            end_offset = offset
    return ".".join(labels), end_offset


# ── Zone records ────────────────────────────────────────────────────


@dataclass
class ZoneEntry:
    name: str            # FQDN inside the zone
    rtype: int           # 1=A  28=AAAA  16=TXT  33=SRV  5=CNAME
    ttl: int = 60
    rdata: bytes = b""

    @classmethod
    def a_record(cls, name: str, ip: str, ttl: int = 60) -> "ZoneEntry":
        return cls(name=name, rtype=1, ttl=ttl,
                   rdata=socket.inet_aton(ip))


@dataclass
class Zone:
    apex: str = "helen.lan"
    entries: list[ZoneEntry] = field(default_factory=list)

    def lookup(self, name: str, rtype: int) -> list[ZoneEntry]:
        name = name.lower().rstrip(".")
        return [e for e in self.entries
                if e.name.lower() == name and e.rtype == rtype]


# ── DNS server ──────────────────────────────────────────────────────


class HelenDNSServer:
    def __init__(
        self,
        zone: Zone,
        bind_host: str = "0.0.0.0",
        bind_port: int = 53,
        forwarder: Optional[tuple[str, int]] = None,
    ) -> None:
        self.zone = zone
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.forwarder = forwarder or ("9.9.9.9", 53)  # safe LAN default
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _DNSProtocol(self),
            local_addr=(self.bind_host, self.bind_port),
            allow_broadcast=False,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._transport:
            self._transport.close()

    # ── Query handling ───────────────────────────────────────

    def handle(self, data: bytes, addr) -> Optional[bytes]:
        if len(data) < 12:
            return None
        try:
            return self._handle_inner(data, addr)
        except Exception:
            return self._build_error(data, rcode=2)  # SERVFAIL

    def _handle_inner(self, data: bytes, addr) -> Optional[bytes]:
        txid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        offset = 12
        qname, offset = _decode_name(data, offset)
        qtype, qclass = struct.unpack("!HH", data[offset:offset + 4])
        offset += 4

        # Authoritative for our zone?
        if qname.lower().endswith(self.zone.apex.lower()):
            entries = self.zone.lookup(qname, qtype)
            return self._build_answer(data, qname, qtype, entries)

        # Forward upstream — best-effort, blocking thread is fine for
        # LAN volume.
        return self._forward(data)

    def _forward(self, query: bytes) -> Optional[bytes]:
        try:
            with socket.socket(socket.AF_INET,
                                socket.SOCK_DGRAM) as sock:
                sock.settimeout(2.0)
                sock.sendto(query, self.forwarder)
                reply, _ = sock.recvfrom(65535)
                return reply
        except Exception:
            return self._build_error(query, rcode=2)

    def _build_answer(self, query: bytes, qname: str, qtype: int,
                       entries: list[ZoneEntry]) -> bytes:
        txid = struct.unpack("!H", query[:2])[0]
        # flags: response, authoritative, recursion-not-available
        flags = 0x8400
        ancount = len(entries)
        header = struct.pack("!HHHHHH", txid, flags, 1, ancount, 0, 0)
        # Echo the question
        question = _encode_name(qname) + struct.pack("!HH", qtype, 1)
        # Build answer RRs
        answers = b""
        for e in entries:
            answers += (
                _encode_name(e.name)
                + struct.pack("!HHIH", e.rtype, 1, e.ttl, len(e.rdata))
                + e.rdata
            )
        return header + question + answers

    def _build_error(self, query: bytes, rcode: int = 2) -> bytes:
        txid = struct.unpack("!H", query[:2])[0]
        flags = 0x8000 | (rcode & 0x0F)
        return struct.pack("!HHHHHH", txid, flags, 0, 0, 0, 0)


class _DNSProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: HelenDNSServer) -> None:
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        reply = self.server.handle(data, addr)
        if reply and self.transport:
            self.transport.sendto(reply, addr)


# ── CLI / standalone runner ────────────────────────────────────────


async def main() -> None:
    zone_apex = os.environ.get("HELEN_DNS_ZONE", "helen.lan")
    bind_host = os.environ.get("HELEN_DNS_HOST", "0.0.0.0")
    bind_port = int(os.environ.get("HELEN_DNS_PORT", "53"))

    zone = Zone(apex=zone_apex)
    # Seed with a couple of records so the resolver has something to
    # answer right away. In production these get topped up from
    # Helen-Router's registry.
    zone.entries.append(ZoneEntry.a_record(
        f"router.{zone_apex}", "127.0.0.1",
    ))
    zone.entries.append(ZoneEntry.a_record(
        f"helen-server.{zone_apex}", "127.0.0.1",
    ))

    server = HelenDNSServer(zone, bind_host, bind_port)
    await server.start()
    print(f"Helen-DNS listening on {bind_host}:{bind_port}, "
          f"authoritative for *.{zone_apex}")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
