"""
Active latency probing — preemptive path-health updates.

The relay chain in ``cluster_mesh`` records latency *passively* as a
side-effect of real traffic. That works fine when relays fire often,
but in steady-state a peer with no current traffic can sit untested
for minutes — and then a real request picks a stale path.

This loop fires lightweight HEAD probes every ``PROBE_INTERVAL_SEC``
to every fresh peer (and every alias on a multi-NIC peer), feeding
the result into ``path_health`` so the next routing decision is made
on data measured in the last 30 seconds, not 5 minutes ago.

Cost
----
* HEAD ``/api/cluster/info`` is ~300 bytes round trip.
* Default interval 30s × ≤ 100 peers × ≤ 4 aliases = ≤ 13 req/s
  cluster-wide — negligible.
* Bounded fan-out: each cycle picks at most ``PROBE_FANOUT`` peers
  per call so a freshly-restarted server doesn't unleash a thundering
  herd of probes.

The loop is fully optional — relay still works if it never runs.
Probing just makes routing smarter.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


PROBE_INTERVAL_SEC = 30.0
PROBE_TIMEOUT_SEC  = 2.0
PROBE_FANOUT       = 50  # max peers probed per cycle


_loop_task: Optional[asyncio.Task] = None
_running = False


# ── Single-peer probe ───────────────────────────────────────────


async def _probe_peer(peer) -> None:
    """Issue one HEAD-equivalent probe per (host, port) on the peer.

    On success, latency feeds ``path_health.record_success``. On
    failure, ``record_failure`` flips the path into the cooldown
    window so subsequent relay calls skip it.
    """
    try:
        import httpx
        from app.services.path_health import get_path_health
        from app.services.peer_registry import peer_registry
    except ImportError:
        return

    health = get_path_health()

    # Build the (host, port) probe set: primary + every alias.
    probes: list[tuple[str, int]] = [(peer.host, peer.port)]
    try:
        meta = await peer_registry.get(peer.node_id)
        for alias in (getattr(meta, "host_aliases", None) or []):
            if alias and (alias, peer.port) not in probes:
                probes.append((alias, peer.port))
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SEC) as client:
        for host, port in probes:
            t0 = time.time()
            try:
                # Use cluster/info — public, light, returns shape we
                # can verify without parsing.
                url = f"http://{host}:{port}/api/cluster/info"
                r = await client.get(url)
                latency_ms = (time.time() - t0) * 1000.0
                if 200 <= r.status_code < 500:
                    # Even 4xx counts as "reachable" for routing — the
                    # box is alive, just refused this request.
                    health.record_success(host, port, latency_ms)
                else:
                    health.record_failure(host, port)
            except Exception:
                health.record_failure(host, port)


# ── Loop ────────────────────────────────────────────────────────


async def _prober_loop() -> None:
    global _running
    _running = True
    logger.info("latency_prober_started",
                interval_sec=PROBE_INTERVAL_SEC, fanout=PROBE_FANOUT)
    try:
        while _running:
            try:
                await _probe_cycle()
            except Exception as e:
                logger.warning("latency_probe_cycle_failed", error=str(e))
            await asyncio.sleep(PROBE_INTERVAL_SEC)
    finally:
        logger.info("latency_prober_stopped")


async def _probe_cycle() -> None:
    from app.services.node_registry import get_registry
    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    if not peers:
        return
    # Random subset so a 1000-peer cluster doesn't probe everyone in
    # one cycle. Over PROBE_INTERVAL_SEC × cycles the coverage is
    # uniform.
    targets = random.sample(peers, k=min(PROBE_FANOUT, len(peers)))
    await asyncio.gather(
        *(_probe_peer(p) for p in targets),
        return_exceptions=True,
    )


def start_latency_prober() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_prober_loop(), name="latency-prober")
    except RuntimeError:
        logger.warning("latency_prober_no_event_loop_yet")


def stop_latency_prober() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
