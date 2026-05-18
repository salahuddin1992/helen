"""
Per-peer federation bandwidth shaper.

Why
---
A bursty federation peer (replaying a backlog after coming back from
a netsplit, or being deliberately abusive) can saturate the LAN
uplink and starve real-time traffic — calls drop, voice gets choppy.
Helen's federation HMAC keeps the bytes legitimate, but doesn't cap
their *rate*.

This module provides a tiny, dependency-free **token bucket**
limiter the federation send-path can consult before transmitting.
One bucket per peer, sized from env. The bucket refills at a
configurable bytes-per-second; when empty, callers either block
until tokens are available or get a "slow down" signal they can
turn into a 429.

Wire shape
----------
Standalone module — no edits to existing federation code. To use,
the federation send path imports :func:`acquire` and awaits it::

    from app.services.federation_shaper import acquire
    await acquire(peer_id, len(payload))
    await session.post(url, content=payload)

When the env var ``HELEN_FEDERATION_BPS_LIMIT`` is unset or zero, the
shaper is a no-op (``acquire`` returns immediately) so the import is
safe to add unconditionally.

Dedicated env vars
------------------
    HELEN_FEDERATION_BPS_LIMIT          bytes/sec per peer (0 = off)
    HELEN_FEDERATION_BURST_BYTES        max bucket capacity (default = 4× limit)
    HELEN_FEDERATION_SHAPER_MAX_WAIT_S  cap on per-call sleep (default 30s);
                                         exceeding it raises ShaperOverloaded
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


class ShaperOverloaded(Exception):
    """Raised when a request would block longer than ``max_wait_s``."""


@dataclass
class _Bucket:
    capacity: float                # max tokens the bucket holds
    refill_rate: float             # tokens per second (== bytes/sec)
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)
    bytes_sent: int = 0
    bytes_throttled: int = 0
    wait_count: int = 0
    total_wait_s: float = 0.0


@dataclass
class ShaperStats:
    peer_id: str
    capacity: float
    refill_rate: float
    tokens_available: float
    bytes_sent: int
    bytes_throttled: int
    wait_count: int
    avg_wait_ms: float

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "capacity": self.capacity,
            "refill_rate_bps": self.refill_rate,
            "tokens_available": self.tokens_available,
            "bytes_sent": self.bytes_sent,
            "bytes_throttled": self.bytes_throttled,
            "wait_count": self.wait_count,
            "avg_wait_ms": self.avg_wait_ms,
        }


class FederationShaper:
    """Per-peer token-bucket bandwidth limiter."""

    def __init__(
        self,
        bytes_per_second: float,
        *,
        burst_bytes: Optional[float] = None,
        max_wait_s: float = 30.0,
    ) -> None:
        self.refill_rate = max(0.0, float(bytes_per_second))
        # Sensible default: 4-second burst at the steady-state rate.
        self.capacity = float(burst_bytes
                                if burst_bytes is not None
                                else max(self.refill_rate * 4.0, 65536.0))
        self.max_wait_s = max(0.0, float(max_wait_s))
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Top-level lock guards the dicts above. Per-peer locks are
        # held during waits so concurrent sends to the *same* peer are
        # serialized fairly.
        self._top_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.refill_rate > 0

    async def _bucket_for(self, peer_id: str) -> tuple[_Bucket, asyncio.Lock]:
        async with self._top_lock:
            if peer_id not in self._buckets:
                self._buckets[peer_id] = _Bucket(
                    capacity=self.capacity,
                    refill_rate=self.refill_rate,
                    tokens=self.capacity,  # start full
                )
                self._locks[peer_id] = asyncio.Lock()
            return self._buckets[peer_id], self._locks[peer_id]

    def _refill(self, b: _Bucket) -> None:
        now = time.monotonic()
        elapsed = now - b.last_refill
        b.last_refill = now
        b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_rate)

    async def acquire(self, peer_id: str, byte_count: int) -> float:
        """Block until ``byte_count`` tokens are available for
        ``peer_id``. Returns the wait duration in seconds (0.0 when
        no waiting was needed). Raises :class:`ShaperOverloaded` if
        the wait would exceed ``max_wait_s``.

        When ``enabled`` is False, returns 0.0 immediately."""
        if not self.enabled or byte_count <= 0:
            return 0.0

        # A single request larger than the bucket capacity: cap the
        # request to capacity for accounting and let it through after
        # one full bucket refill, otherwise we'd loop forever.
        request = float(min(byte_count, self.capacity))

        bucket, lock = await self._bucket_for(peer_id)
        async with lock:
            self._refill(bucket)
            wait_total = 0.0
            while bucket.tokens < request:
                deficit = request - bucket.tokens
                # Time until deficit refills:
                wait_s = deficit / bucket.refill_rate
                if wait_total + wait_s > self.max_wait_s:
                    bucket.bytes_throttled += byte_count
                    raise ShaperOverloaded(
                        f"peer {peer_id}: would wait "
                        f"{wait_total + wait_s:.2f}s "
                        f"(cap {self.max_wait_s:.1f}s)",
                    )
                await asyncio.sleep(wait_s)
                wait_total += wait_s
                self._refill(bucket)

            bucket.tokens -= request
            bucket.bytes_sent += byte_count
            if wait_total > 0:
                bucket.wait_count += 1
                bucket.total_wait_s += wait_total
            return wait_total

    def stats_for(self, peer_id: str) -> Optional[ShaperStats]:
        b = self._buckets.get(peer_id)
        if not b:
            return None
        avg_wait_ms = (
            (b.total_wait_s / b.wait_count) * 1000.0
            if b.wait_count else 0.0
        )
        return ShaperStats(
            peer_id=peer_id,
            capacity=b.capacity,
            refill_rate=b.refill_rate,
            tokens_available=b.tokens,
            bytes_sent=b.bytes_sent,
            bytes_throttled=b.bytes_throttled,
            wait_count=b.wait_count,
            avg_wait_ms=avg_wait_ms,
        )

    def all_stats(self) -> list[ShaperStats]:
        return [self.stats_for(p) for p in self._buckets]  # type: ignore[list-item]


# ── Singleton ─────────────────────────────────────────────────────


_shaper: Optional[FederationShaper] = None


def configure_federation_shaper(
    bytes_per_second: float,
    *,
    burst_bytes: Optional[float] = None,
    max_wait_s: float = 30.0,
) -> FederationShaper:
    global _shaper
    _shaper = FederationShaper(
        bytes_per_second,
        burst_bytes=burst_bytes,
        max_wait_s=max_wait_s,
    )
    return _shaper


def get_federation_shaper() -> Optional[FederationShaper]:
    return _shaper


async def acquire(peer_id: str, byte_count: int) -> float:
    """Process-wide convenience wrapper. Returns 0.0 (no wait) if
    the shaper hasn't been configured."""
    if _shaper is None:
        return 0.0
    return await _shaper.acquire(peer_id, byte_count)


def shutdown_federation_shaper() -> None:
    global _shaper
    _shaper = None


def configure_from_env() -> Optional[FederationShaper]:
    bps = float(os.environ.get("HELEN_FEDERATION_BPS_LIMIT", "0") or "0")
    if bps <= 0:
        return None
    burst_raw = os.environ.get("HELEN_FEDERATION_BURST_BYTES", "")
    burst = float(burst_raw) if burst_raw else None
    max_wait = float(os.environ.get(
        "HELEN_FEDERATION_SHAPER_MAX_WAIT_S", "30",
    ))
    s = configure_federation_shaper(
        bps, burst_bytes=burst, max_wait_s=max_wait,
    )
    logger.info("federation_shaper_configured",
                rate_bps=int(bps),
                capacity=int(s.capacity),
                max_wait_s=max_wait)
    return s


__all__ = [
    "FederationShaper",
    "ShaperOverloaded",
    "ShaperStats",
    "configure_federation_shaper",
    "get_federation_shaper",
    "shutdown_federation_shaper",
    "configure_from_env",
    "acquire",
]
