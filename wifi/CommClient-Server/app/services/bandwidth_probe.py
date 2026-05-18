"""
Bandwidth probing — actual throughput between peers.

``latency_prober`` measures RTT but a 5ms ping with a saturated
upstream is still a bad path for a file transfer. This module
periodically pushes a small payload through ``/api/cluster/bandwidth-probe``
and measures bytes/second to keep an EWMA bandwidth estimate per
``host:port``.

Probe size is deliberately small (default 64 KiB) to:
  * Stay below typical TCP slow-start window so the result reflects
    sustained throughput, not burst capacity.
  * Add negligible overhead even when scaled across hundreds of
    peers (default 64 KiB × 50 peers / 60s = 53 KiB/s cluster-wide).

The estimate feeds into ``load_balancer`` as a future signal — for
now it's stored and exposed via the admin endpoint, ready to plug
in when bulk-transfer routing is added.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


PROBE_INTERVAL_SEC = 60.0
PROBE_TIMEOUT_SEC  = 10.0
PROBE_FANOUT       = 8
PROBE_BYTES        = int(os.environ.get("HELEN_BW_PROBE_BYTES", str(64 * 1024)))
EWMA_ALPHA         = 0.3


# ── In-memory bandwidth tracker ─────────────────────────────────


class BandwidthTracker:
    _singleton: "BandwidthTracker | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # key = "host:port", value = dict(mbps_ewma, samples, last_at)
        self._stats: dict[str, dict] = {}

    @classmethod
    def instance(cls) -> "BandwidthTracker":
        if cls._singleton is None:
            cls._singleton = BandwidthTracker()
        return cls._singleton

    @staticmethod
    def _key(host: str, port: int) -> str:
        return f"{host}:{int(port)}"

    def record(self, host: str, port: int, mbps: float) -> None:
        k = self._key(host, port)
        with self._lock:
            row = self._stats.setdefault(
                k, {"mbps_ewma": mbps, "samples": 0, "last_at": 0.0},
            )
            if row["samples"] == 0:
                row["mbps_ewma"] = mbps
            else:
                row["mbps_ewma"] = (
                    EWMA_ALPHA * mbps
                    + (1.0 - EWMA_ALPHA) * row["mbps_ewma"]
                )
            row["samples"] += 1
            row["last_at"]  = time.time()

    def get(self, host: str, port: int) -> Optional[float]:
        with self._lock:
            row = self._stats.get(self._key(host, port))
            return row["mbps_ewma"] if row else None

    def evict_stale(self, max_age_sec: float = 600.0) -> int:
        """Drop entries we haven't probed for ``max_age_sec`` seconds.
        Without this, peers that left the cluster keep an entry in the
        EWMA dict forever — eventually leaking a few KB per departed
        peer. Returns count evicted."""
        cutoff = time.time() - max_age_sec
        with self._lock:
            stale = [k for k, v in self._stats.items()
                     if (v.get("last_at") or 0.0) < cutoff]
            for k in stale:
                self._stats.pop(k, None)
        return len(stale)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "probe_bytes": PROBE_BYTES,
                "tracked": [
                    {
                        "key":      k,
                        "mbps":     round(v["mbps_ewma"], 2),
                        "samples":  v["samples"],
                        "last_age_s": round(time.time() - v["last_at"], 1)
                            if v["last_at"] else None,
                    }
                    for k, v in sorted(self._stats.items())
                ],
            }


def get_bandwidth() -> BandwidthTracker:
    return BandwidthTracker.instance()


# ── Probe a single peer ─────────────────────────────────────────


async def _probe_peer(peer) -> Optional[float]:
    """POST a payload of PROBE_BYTES random bytes and measure mbps."""
    try:
        import httpx
    except ImportError:
        return None
    payload = os.urandom(PROBE_BYTES)
    url = f"http://{peer.host}:{peer.port}/api/cluster/bandwidth-probe"
    try:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SEC) as c:
            r = await c.post(url, content=payload,
                             headers={"Content-Type": "application/octet-stream"})
        elapsed = time.time() - t0
        if r.status_code != 200 or elapsed <= 0:
            return None
        # bits per second = bytes × 8 / seconds; mbps = bps / 1_000_000
        mbps = (PROBE_BYTES * 8) / elapsed / 1_000_000.0
        get_bandwidth().record(peer.host, peer.port, mbps)
        return mbps
    except Exception as e:
        logger.debug("bandwidth_probe_failed",
                     peer=peer.node_id[:24], error=str(e)[:80])
        return None


# ── Loop ────────────────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _probe_loop() -> None:
    global _running
    _running = True
    logger.info(
        "bandwidth_probe_started",
        interval_sec=PROBE_INTERVAL_SEC,
        fanout=PROBE_FANOUT,
        probe_bytes=PROBE_BYTES,
    )
    try:
        while _running:
            try:
                await _probe_cycle()
            except Exception as e:
                logger.warning("bandwidth_probe_cycle_failed", error=str(e))
            await asyncio.sleep(PROBE_INTERVAL_SEC)
    finally:
        logger.info("bandwidth_probe_stopped")


async def _probe_cycle() -> None:
    from app.services.node_registry import get_registry
    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    if not peers:
        return
    targets = random.sample(peers, k=min(PROBE_FANOUT, len(peers)))
    await asyncio.gather(
        *(_probe_peer(p) for p in targets),
        return_exceptions=True,
    )


def start_bandwidth_probe() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_probe_loop(), name="bandwidth-probe")
    except RuntimeError:
        logger.warning("bandwidth_probe_no_event_loop_yet")


def stop_bandwidth_probe() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
