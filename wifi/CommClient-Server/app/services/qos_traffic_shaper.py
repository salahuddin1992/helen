"""QoS traffic shaper — token-bucket per traffic class.

Three classes, each with its own bandwidth budget:

  * INTERACTIVE  — chat, presence, signaling.   default 80% of NIC.
  * BULK         — file transfers, backups.     default 15% of NIC.
  * BACKGROUND   — gossip, metrics, state-sync.  default 5%.

Senders ``await shaper.acquire(class, bytes_count)`` before
writing. The shaper blocks until enough tokens have refilled. This
turns the NIC into a fair-share scheduler so a 10 GB file upload
can't starve a chat ping.

Implementation: simple async token bucket per class, refilled at
``rate_bps`` once per second. No need for a separate dispatcher
loop; tokens are added on-demand based on elapsed time.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from enum import Enum
from typing import Optional


class TrafficClass(str, Enum):
    INTERACTIVE = "interactive"
    BULK        = "bulk"
    BACKGROUND  = "background"


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


# Default budget split (must sum ≤ 1.0).
DEFAULT_SHARE = {
    TrafficClass.INTERACTIVE: _f("HELEN_QOS_INTERACTIVE_PCT", 0.80),
    TrafficClass.BULK:        _f("HELEN_QOS_BULK_PCT",        0.15),
    TrafficClass.BACKGROUND:  _f("HELEN_QOS_BACKGROUND_PCT",  0.05),
}


def _local_nic_bps() -> float:
    """Best-effort detection of the fastest local NIC, in bytes/sec."""
    try:
        import psutil
        stats = psutil.net_if_stats()
        speeds = [s.speed for n, s in stats.items()
                  if s.isup and not n.lower().startswith(("lo", "loopback"))
                  and s.speed > 0]
        if speeds:
            mbps = max(speeds)
            return mbps * 1_000_000.0 / 8.0
    except Exception:
        pass
    return 100_000_000.0 / 8.0  # 100 Mbps fallback


class _Bucket:
    __slots__ = ("rate_bps", "capacity", "tokens", "last_refill")

    def __init__(self, rate_bps: float) -> None:
        self.rate_bps = float(rate_bps)
        self.capacity = float(rate_bps)  # 1 second worth
        self.tokens = self.capacity
        self.last_refill = time.time()

    def _refill(self) -> None:
        now = time.time()
        delta = now - self.last_refill
        if delta <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + delta * self.rate_bps)
        self.last_refill = now

    def take(self, n: int) -> float:
        """Returns 0 if tokens were available, or seconds to wait
        before the next take attempt."""
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        deficit = n - self.tokens
        return deficit / self.rate_bps


class TrafficShaper:
    _singleton: "TrafficShaper | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        nic_bps = _local_nic_bps()
        self._buckets: dict[TrafficClass, _Bucket] = {
            cls: _Bucket(nic_bps * share)
            for cls, share in DEFAULT_SHARE.items()
        }
        self._stats: dict[str, dict] = {
            cls.value: {"requests": 0, "bytes": 0, "wait_total_sec": 0.0}
            for cls in TrafficClass
        }

    @classmethod
    def instance(cls) -> "TrafficShaper":
        if cls._singleton is None:
            cls._singleton = TrafficShaper()
        return cls._singleton

    async def acquire(self, traffic_class: TrafficClass | str,
                      bytes_count: int) -> float:
        """Block until ``bytes_count`` tokens are available for
        ``traffic_class``. Returns the seconds waited."""
        if isinstance(traffic_class, str):
            try:
                traffic_class = TrafficClass(traffic_class)
            except ValueError:
                traffic_class = TrafficClass.INTERACTIVE
        if bytes_count <= 0:
            return 0.0

        wait_total = 0.0
        while True:
            with self._lock:
                bucket = self._buckets[traffic_class]
                wait = bucket.take(bytes_count)
            if wait <= 0:
                with self._lock:
                    self._stats[traffic_class.value]["requests"] += 1
                    self._stats[traffic_class.value]["bytes"] += bytes_count
                    self._stats[traffic_class.value]["wait_total_sec"] += wait_total
                return wait_total
            await asyncio.sleep(min(wait, 0.5))
            wait_total += min(wait, 0.5)

    def reconfigure(self, total_bps: float | None = None,
                    share: dict[TrafficClass, float] | None = None) -> None:
        """Override the budget at runtime (admin endpoint)."""
        with self._lock:
            new_total = total_bps if total_bps is not None else _local_nic_bps()
            new_share = share or DEFAULT_SHARE
            for cls in TrafficClass:
                rate = new_total * float(new_share.get(cls, 0.0))
                if rate > 0:
                    self._buckets[cls] = _Bucket(rate)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "buckets": {
                    cls.value: {
                        "rate_bps":  round(self._buckets[cls].rate_bps, 1),
                        "tokens":    round(self._buckets[cls].tokens, 1),
                        "capacity":  round(self._buckets[cls].capacity, 1),
                    }
                    for cls in TrafficClass
                },
                "stats":   {k: dict(v) for k, v in self._stats.items()},
            }


def get_shaper() -> TrafficShaper:
    return TrafficShaper.instance()
