"""Stale-entry reaper — TTL-based eviction loop.

Runs every ``reaper_interval_sec`` and:

  * Marks records with no heartbeat in ``ttl + grace`` as
    ``UNHEALTHY``.
  * Deletes records past ``2 × (ttl + grace)`` (assumed dead).
  * Persists to disk if anything changed.

Emits ``service.expired`` events that the resilience / monitoring
package can listen on for alerts.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.core.logging import get_logger
from app.service_discovery.discovery_config import get_config
from app.service_discovery.discovery_events import emit
from app.service_discovery.service_record import ServiceStatus
from app.service_discovery.service_registry import get_registry

logger = get_logger(__name__)


_loop_task: Optional[asyncio.Task] = None
_running = False
_stats = {"marks": 0, "evictions": 0, "cycles": 0}


def reap_once() -> dict:
    cfg = get_config()
    reg = get_registry()
    now = time.time()
    grace = cfg.heartbeat_grace_sec

    marked = 0
    evicted = 0
    for record in reg.all():
        age = now - record.last_heartbeat_at
        ttl_with_grace = record.ttl_sec + grace
        if record.status == ServiceStatus.HEALTHY and age > ttl_with_grace:
            record.status = ServiceStatus.UNHEALTHY
            marked += 1
            emit("service.expired", {
                "service_id":   record.service_id[:24],
                "type":         record.service_type.value,
                "age_sec":      round(age, 1),
            })
        if age > 2 * ttl_with_grace:
            if reg.deregister(record.service_id):
                evicted += 1

    _stats["cycles"] += 1
    _stats["marks"] += marked
    _stats["evictions"] += evicted
    if marked or evicted:
        reg.persist_if_dirty()
    return {"marked": marked, "evicted": evicted}


async def _run_loop() -> None:
    global _running
    cfg = get_config()
    _running = True
    logger.info("sd_reaper_started",
                interval_sec=cfg.reaper_interval_sec)
    try:
        while _running:
            try:
                reap_once()
            except Exception as e:
                logger.warning("sd_reaper_cycle_failed", error=str(e))
            await asyncio.sleep(cfg.reaper_interval_sec)
    finally:
        logger.info("sd_reaper_stopped")


def start() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_run_loop(), name="sd-reaper")
    except RuntimeError:
        logger.warning("sd_reaper_no_event_loop_yet")


def stop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def stats() -> dict:
    return {
        "running": _running,
        **dict(_stats),
    }
