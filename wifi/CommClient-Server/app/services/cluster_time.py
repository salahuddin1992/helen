"""
Cluster time synchronization — bounded clock skew between peers.

The HMAC replay window is 60 seconds. If two servers' clocks drift
more than that the federation handshake breaks even though both keys
are right. NTP usually keeps machines within a few hundred ms, but:

  * On Windows boxes without internet, w32time may stop syncing.
  * Inside containers, host time can drift by minutes during pause/
    resume cycles.
  * On industrial / air-gapped LANs, no NTP at all.

This module gives the cluster its own monotonic time consensus. Each
peer publishes its clock; everyone else samples it and computes a
median offset. We don't rewrite system time — we just expose
``cluster_time.now()`` which any HMAC signer / verifier can use
instead of ``time.time()`` to dampen drift.

Algorithm (Cristian-style with median)
--------------------------------------
1. Every ``SYNC_INTERVAL_SEC`` we pick K peers.
2. For each: send T₀, receive (T_peer, T₁=now). Round-trip RTT = T₁-T₀.
3. Estimated peer time at our T₁ = T_peer + RTT/2.
4. Offset = peer_time - our_time.
5. Cluster offset = median of valid samples.
6. ``cluster_time.now() = system_time + cluster_offset``.

We use median (not mean) so a single peer with a wildly wrong clock
can't poison the consensus.

Bounds
------
* If consensus offset > 30s, we log a warning but apply it anyway —
  the alternative is letting HMAC verify keep failing.
* If we can't reach any peer for SYNC_INTERVAL_SEC × 5, we hold the
  last good offset (don't reset to 0).

Cost
----
One HEAD-equivalent probe per peer per SYNC_INTERVAL_SEC × K — same
order as latency_prober. Negligible.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import threading
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


SYNC_INTERVAL_SEC = 60.0
SAMPLE_FANOUT     = 5
SAMPLE_TIMEOUT    = 1.5
WARN_THRESHOLD_SEC = 30.0


class ClusterTime:
    _singleton: "ClusterTime | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._offset_sec: float = 0.0
        self._last_sync_at: float = 0.0
        self._last_sample_count: int = 0
        self._consecutive_failures: int = 0

    @classmethod
    def instance(cls) -> "ClusterTime":
        if cls._singleton is None:
            cls._singleton = ClusterTime()
        return cls._singleton

    def now(self) -> float:
        """Cluster-consensus unix time. Use this in HMAC signers /
        verifiers in place of ``time.time()`` for cross-peer
        operations; for local-only timestamps ``time.time()`` is
        still correct."""
        with self._lock:
            return time.time() + self._offset_sec

    def offset(self) -> float:
        with self._lock:
            return self._offset_sec

    def update_offset(self, samples: list[float]) -> None:
        """Compute the median of new samples and update the consensus
        offset. Old offset stays in place if no samples arrive."""
        if not samples:
            with self._lock:
                self._consecutive_failures += 1
                fails = self._consecutive_failures
            # Match the documented behavior: warn the operator after 5
            # cycles of total reachability failure so they know the
            # cluster_time offset is staying frozen at its last value.
            # Warn once at the boundary, then again every 10 cycles to
            # avoid log spam.
            if fails == 5 or (fails > 5 and fails % 10 == 0):
                logger.warning(
                    "cluster_time_no_peers_reachable",
                    consecutive_failures=fails,
                    holding_offset_sec=round(self._offset_sec, 3),
                )
            return
        new_offset = statistics.median(samples)
        with self._lock:
            old = self._offset_sec
            self._offset_sec = new_offset
            self._last_sync_at = time.time()
            self._last_sample_count = len(samples)
            self._consecutive_failures = 0
            if abs(new_offset) > WARN_THRESHOLD_SEC:
                logger.warning(
                    "cluster_time_large_offset",
                    offset_sec=round(new_offset, 3),
                    samples=len(samples),
                )
            if abs(new_offset - old) > 1.0:
                logger.info(
                    "cluster_time_offset_changed",
                    old=round(old, 3),
                    new=round(new_offset, 3),
                    samples=len(samples),
                )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "offset_sec":          round(self._offset_sec, 3),
                "cluster_time":        round(time.time() + self._offset_sec, 3),
                "system_time":         round(time.time(), 3),
                "last_sync_at":        self._last_sync_at,
                "last_sample_count":   self._last_sample_count,
                "consecutive_failures": self._consecutive_failures,
            }


def get_cluster_time() -> ClusterTime:
    return ClusterTime.instance()


# ── Sample collection ──────────────────────────────────────────


async def _sample_peer(peer) -> Optional[float]:
    """Returns offset (peer_time - our_time) or None on failure."""
    try:
        import httpx
    except ImportError:
        return None
    try:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=SAMPLE_TIMEOUT) as c:
            r = await c.get(f"http://{peer.host}:{peer.port}/api/cluster/time")
            t1 = time.time()
        if r.status_code != 200:
            return None
        d = r.json() or {}
        peer_time = float(d.get("now") or 0.0)
        if peer_time <= 0:
            return None
        rtt = t1 - t0
        # Cristian's adjustment: peer time at our t1 ≈ peer_time + rtt/2
        peer_time_at_t1 = peer_time + (rtt / 2.0)
        return peer_time_at_t1 - t1
    except Exception:
        return None


# ── Loop ───────────────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _sync_loop() -> None:
    global _running
    _running = True
    logger.info("cluster_time_sync_started",
                interval_sec=SYNC_INTERVAL_SEC, fanout=SAMPLE_FANOUT)
    try:
        while _running:
            try:
                await _sync_once()
            except Exception as e:
                logger.warning("cluster_time_sync_failed", error=str(e))
            await asyncio.sleep(SYNC_INTERVAL_SEC)
    finally:
        logger.info("cluster_time_sync_stopped")


async def _sync_once() -> None:
    from app.services.node_registry import get_registry
    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    if not peers:
        return
    targets = random.sample(peers, k=min(SAMPLE_FANOUT, len(peers)))
    samples = await asyncio.gather(
        *(_sample_peer(p) for p in targets),
        return_exceptions=True,
    )
    valid = [s for s in samples if isinstance(s, (int, float))]
    get_cluster_time().update_offset(valid)


def start_cluster_time_sync() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_sync_loop(), name="cluster-time-sync")
    except RuntimeError:
        logger.warning("cluster_time_no_event_loop_yet")


def stop_cluster_time_sync() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
