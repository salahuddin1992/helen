"""
Pi-hole-style filtering DNS for Helen.

What this adds on top of ``internal_dns.HelenDNSServer``
--------------------------------------------------------
``internal_dns`` is enough to serve ``*.helen.lan`` and forward
everything else to a single upstream. This module adds:

  * **Domain blocklist** — refuse-with-NXDOMAIN any name in
    ``HELEN_DNS_BLOCKLIST`` (one host per line, ``#`` comments). Same
    file format Pi-hole uses, so operators can drop in one of the
    well-known lists (StevenBlack/hosts, etc.) without conversion.

  * **Multi-upstream forwarder with fallback** — try each of
    ``HELEN_DNS_UPSTREAMS`` in order. First one to answer wins; if
    they all fail we return SERVFAIL.

  * **Tiny in-memory answer cache** — TTL-aware, capped at
    ``HELEN_DNS_CACHE_MAX`` entries (default 1024). Cuts the
    per-query latency for repeated lookups (most LAN browsers reissue
    every page-load).

  * **Per-domain stats** — tracks {queries, blocks, cache_hits,
    upstream_misses} so the admin UI can show "how much DNS noise is
    actually being filtered."

Entirely self-hosted. No call-home, no telemetry, no DoH.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.internal_dns import (
    HelenDNSServer, Zone, _decode_name, _encode_name,
)


# ── Blocklist loader ───────────────────────────────────────────────


def load_blocklist(path: str) -> set[str]:
    """Parse a hosts-file-style blocklist. Returns a set of lowercase
    FQDNs. Tolerates: comments (``#``), blank lines, ``0.0.0.0`` /
    ``127.0.0.1`` prefixes, inline comments after the host."""
    blocked: set[str] = set()
    p = Path(path)
    if not p.is_file():
        return blocked
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip an inline comment.
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        parts = line.split()
        if not parts:
            continue
        # Hosts-format: "0.0.0.0 ads.example.com"  →  domain is parts[1]
        # Plain-format: "ads.example.com"          →  domain is parts[0]
        if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1", "::"):
            domain = parts[1].lower().rstrip(".")
        else:
            domain = parts[0].lower().rstrip(".")
        if "." in domain and not domain.startswith("local"):
            blocked.add(domain)
    return blocked


# ── LRU cache ──────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    payload: bytes          # full reply bytes (already-formed)
    expires_at: float


class _DNSCache:
    def __init__(self, max_entries: int = 1024) -> None:
        self.max = max_entries
        self._data: "OrderedDict[tuple[str, int], _CacheEntry]" = OrderedDict()

    def get(self, key: tuple[str, int]) -> Optional[bytes]:
        e = self._data.get(key)
        if not e:
            return None
        if e.expires_at < time.time():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return e.payload

    def put(self, key: tuple[str, int], payload: bytes,
             ttl: int = 300) -> None:
        self._data[key] = _CacheEntry(
            payload=payload, expires_at=time.time() + max(1, ttl),
        )
        self._data.move_to_end(key)
        while len(self._data) > self.max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def size(self) -> int:
        return len(self._data)


# ── Stats ──────────────────────────────────────────────────────────


@dataclass
class DNSStats:
    queries: int = 0
    cache_hits: int = 0
    blocked: int = 0
    upstream_hits: int = 0
    upstream_misses: int = 0
    by_domain: dict[str, int] = field(default_factory=dict)

    def record_query(self, name: str) -> None:
        self.queries += 1
        self.by_domain[name] = self.by_domain.get(name, 0) + 1

    def to_dict(self) -> dict:
        # Top-20 noisy domains.
        top = sorted(self.by_domain.items(),
                      key=lambda kv: kv[1], reverse=True)[:20]
        return {
            "queries": self.queries,
            "cache_hits": self.cache_hits,
            "blocked": self.blocked,
            "upstream_hits": self.upstream_hits,
            "upstream_misses": self.upstream_misses,
            "block_rate": (self.blocked / self.queries) if self.queries else 0,
            "top_domains": [{"name": n, "count": c} for n, c in top],
        }


# ── Recursive resolver ─────────────────────────────────────────────


class RecursiveDNSServer(HelenDNSServer):
    """``HelenDNSServer`` with blocklist + multi-upstream + cache.

    Drop-in replacement: same constructor signature plus the new
    knobs. ``handle()`` short-circuits blocked names with NXDOMAIN
    before they reach the upstream forwarder.
    """

    def __init__(
        self,
        zone: Zone,
        bind_host: str = "0.0.0.0",
        bind_port: int = 53,
        forwarder: Optional[tuple[str, int]] = None,
        *,
        upstreams: Optional[list[tuple[str, int]]] = None,
        blocklist: Optional[set[str]] = None,
        cache_max: int = 1024,
    ) -> None:
        super().__init__(zone, bind_host, bind_port, forwarder)
        # Multi-upstream — keep `forwarder` for compatibility with
        # the parent's `_forward()`, but our override uses the list.
        self.upstreams = upstreams or [self.forwarder]
        self.blocklist = blocklist or set()
        self.cache = _DNSCache(max_entries=cache_max)
        self.stats = DNSStats()

    # The parent class's handle() calls _handle_inner; we override the
    # zone-vs-forward branch to inject blocking + caching.
    def _handle_inner(self, data: bytes, addr) -> Optional[bytes]:
        txid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        offset = 12
        qname, offset = _decode_name(data, offset)
        qtype, qclass = struct.unpack("!HH", data[offset:offset + 4])

        qname_l = qname.lower().rstrip(".")
        self.stats.record_query(qname_l)

        # 1. Authoritative for our zone? (parent path)
        if qname_l.endswith(self.zone.apex.lower()):
            entries = self.zone.lookup(qname, qtype)
            return self._build_answer(data, qname, qtype, entries)

        # 2. Blocked?
        if self._is_blocked(qname_l):
            self.stats.blocked += 1
            return self._build_nxdomain(data)

        # 3. Cache hit?
        cache_key = (qname_l, qtype)
        cached = self.cache.get(cache_key)
        if cached:
            self.stats.cache_hits += 1
            # Rewrite the txid so the client matches the response.
            return data[:2] + cached[2:]

        # 4. Try each upstream until one answers.
        reply = self._forward_multi(data)
        if reply:
            self.stats.upstream_hits += 1
            ttl = self._extract_min_ttl(reply) or 300
            self.cache.put(cache_key, reply, ttl=ttl)
            return reply
        self.stats.upstream_misses += 1
        return self._build_error(data, rcode=2)

    def _is_blocked(self, qname: str) -> bool:
        # Match the exact host AND any parent suffix, so blocking
        # ``ads.example.com`` also blocks ``a.b.ads.example.com``.
        if qname in self.blocklist:
            return True
        parts = qname.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[i:]) in self.blocklist:
                return True
        return False

    def _build_nxdomain(self, query: bytes) -> bytes:
        txid = struct.unpack("!H", query[:2])[0]
        # Response, RD copied from query, RA=1, RCODE=3 (NXDOMAIN)
        flags_q = struct.unpack("!H", query[2:4])[0]
        rd = flags_q & 0x0100
        flags = 0x8080 | rd | 0x03
        # Echo the question section.
        offset = 12
        _, end = _decode_name(query, offset)
        question = query[12:end + 4]  # name + qtype + qclass
        return struct.pack("!HHHHHH",
                            txid, flags, 1, 0, 0, 0) + question

    def _forward_multi(self, query: bytes) -> Optional[bytes]:
        for ups in self.upstreams:
            try:
                with socket.socket(socket.AF_INET,
                                    socket.SOCK_DGRAM) as sock:
                    sock.settimeout(2.0)
                    sock.sendto(query, ups)
                    reply, _ = sock.recvfrom(65535)
                    if reply:
                        return reply
            except Exception:
                continue
        return None

    def _extract_min_ttl(self, reply: bytes) -> Optional[int]:
        """Best-effort TTL parser for the answer section. Returns the
        smallest TTL across answer RRs, clamped to [10, 3600]. We only
        need this to decide cache lifetime — wire-format edge cases
        (compression in RDATA, OPT RRs) just yield None and we use the
        default."""
        try:
            if len(reply) < 12:
                return None
            an = struct.unpack("!H", reply[6:8])[0]
            if an == 0:
                return None
            offset = 12
            _, offset = _decode_name(reply, offset)
            offset += 4  # qtype + qclass
            min_ttl: Optional[int] = None
            for _ in range(an):
                _, offset = _decode_name(reply, offset)
                if offset + 10 > len(reply):
                    return min_ttl
                _, _, ttl, rdlen = struct.unpack(
                    "!HHIH", reply[offset:offset + 10],
                )
                offset += 10 + rdlen
                if min_ttl is None or ttl < min_ttl:
                    min_ttl = ttl
            if min_ttl is None:
                return None
            return max(10, min(3600, min_ttl))
        except Exception:
            return None


# ── Env-driven factory ─────────────────────────────────────────────


def _parse_upstreams(value: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for tok in value.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            host, port = tok.rsplit(":", 1)
            out.append((host.strip(), int(port)))
        else:
            out.append((tok, 53))
    return out


def build_recursive_server_from_env(
    zone: Zone,
) -> RecursiveDNSServer:
    """Construct a ready-to-start ``RecursiveDNSServer`` from
    ``HELEN_DNS_*`` env vars."""
    upstreams = _parse_upstreams(
        os.environ.get("HELEN_DNS_UPSTREAMS", "9.9.9.9:53,1.1.1.1:53"),
    )
    blocklist = set()
    bp = os.environ.get("HELEN_DNS_BLOCKLIST")
    if bp:
        blocklist = load_blocklist(bp)
    cache_max = int(os.environ.get("HELEN_DNS_CACHE_MAX", "1024"))
    bind_host = os.environ.get("HELEN_DNS_HOST", "0.0.0.0")
    bind_port = int(os.environ.get("HELEN_DNS_PORT", "53"))
    return RecursiveDNSServer(
        zone, bind_host=bind_host, bind_port=bind_port,
        upstreams=upstreams, blocklist=blocklist, cache_max=cache_max,
    )


__all__ = [
    "RecursiveDNSServer",
    "DNSStats",
    "load_blocklist",
    "build_recursive_server_from_env",
]
