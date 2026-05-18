"""
Network partition detection — split-brain awareness for the cluster.

A "partition" is when two halves of the cluster can each see internal
peers but can't reach the other half. Without detection, both halves
continue accepting writes, and when the network heals, the two
diverged states have to be reconciled — sometimes with conflicts.

This module makes partition state visible:

  * **Quorum sense:** count fresh peers vs. last-known-cluster-size.
    If we can see < ⌈N/2⌉ + 1, we are minority; flip a flag.
  * **State exposure:** ``/api/cluster/partition-state`` for ops.
  * **Behavioural hook:** when in minority, optionally degrade to
    read-mostly (writes still accepted but flagged for post-heal
    re-verification). Off by default — operators flip it on with
    ``HELEN_MINORITY_READ_ONLY=1``.
  * **Heal detection:** when fresh peer count crosses back over
    quorum, emit ``partition_healed`` event so the reconciliation
    loop can run an aggressive convergence pass.

This is *detection*, not *prevention* — the design choice is
availability over consistency on a LAN (partitions are rare and
short, so we keep accepting writes and let reconciliation merge).

Quorum math
-----------
N is the high-water mark of fresh peers we've ever seen (saved to
disk). Default quorum threshold = ⌈N/2⌉ + 1. With N=10 we need ≥ 6
peers visible to be the majority side.

Edge cases
----------
* Cold start (N=1): we are always our own quorum.
* New peer joins during partition: high-water rises afterwards, may
  briefly classify both halves as minority. Self-correcting next
  cycle.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_HIGH_WATER_FILE = _DATA_DIR / "cluster_high_water.json"

CHECK_INTERVAL_SEC = 10.0


class PartitionState:
    """Singleton — observable cluster-quorum state."""

    _singleton: "PartitionState | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._high_water: int = 1  # at minimum we count ourselves
        self._fresh_count: int = 1
        self._is_majority: bool = True
        self._partition_started_at: Optional[float] = None
        self._partition_ended_at: Optional[float] = None
        self._minority_read_only: bool = bool(
            os.environ.get("HELEN_MINORITY_READ_ONLY", "")
        )
        self._load_high_water()

    @classmethod
    def instance(cls) -> "PartitionState":
        if cls._singleton is None:
            cls._singleton = PartitionState()
        return cls._singleton

    def _load_high_water(self) -> None:
        try:
            if _HIGH_WATER_FILE.is_file():
                d = json.loads(_HIGH_WATER_FILE.read_text(encoding="utf-8"))
                self._high_water = max(1, int(d.get("high_water", 1)))
        except Exception:
            pass

    def _persist_high_water(self) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _HIGH_WATER_FILE.write_text(
                json.dumps({"high_water": self._high_water,
                            "updated_at": time.time()}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("partition_high_water_persist_failed", error=str(e))

    @staticmethod
    def quorum_threshold(n: int) -> int:
        """⌈N/2⌉ + 1 — strict majority."""
        return math.floor(n / 2) + 1

    # ── Public API ──────────────────────────────────────────

    def update(self, fresh_peer_count: int) -> dict:
        """Called every CHECK_INTERVAL_SEC by the loop."""
        # +1 for self — the count comes in as peers-only.
        n_visible = fresh_peer_count + 1
        with self._lock:
            # Update high-water mark (only ratchet up).
            if n_visible > self._high_water:
                self._high_water = n_visible
                self._persist_high_water()

            threshold = self.quorum_threshold(self._high_water)
            was_majority = self._is_majority
            now = time.time()

            self._fresh_count = n_visible
            self._is_majority = (n_visible >= threshold)

            if was_majority and not self._is_majority:
                self._partition_started_at = now
                self._partition_ended_at = None
                logger.warning(
                    "partition_detected",
                    visible=n_visible, expected=self._high_water,
                    threshold=threshold,
                )
            elif (not was_majority) and self._is_majority:
                self._partition_ended_at = now
                logger.info(
                    "partition_healed",
                    visible=n_visible, expected=self._high_water,
                    threshold=threshold,
                    duration_sec=(
                        round(now - self._partition_started_at, 1)
                        if self._partition_started_at else None
                    ),
                )
                self._on_heal()

            return self.snapshot_locked()

    def _on_heal(self) -> None:
        """Trigger an aggressive reconciliation pass when partition
        heals. Runs in background; no exceptions propagate."""
        try:
            import asyncio as _a
            from app.services.state_reconciliation import _reconcile_once
            from app.services.anti_entropy import _cycle as ae_cycle
            try:
                loop = _a.get_event_loop()
                loop.create_task(_reconcile_once())
                loop.create_task(ae_cycle())
            except RuntimeError:
                pass
        except Exception:
            pass

    def is_majority(self) -> bool:
        with self._lock:
            return self._is_majority

    def is_read_only(self) -> bool:
        """True when we should refuse writes (minority + flag set)."""
        with self._lock:
            return (not self._is_majority) and self._minority_read_only

    def snapshot(self) -> dict:
        with self._lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict:
        return {
            "is_majority":            self._is_majority,
            "fresh_count":            self._fresh_count,
            "high_water":             self._high_water,
            "quorum_threshold":       self.quorum_threshold(self._high_water),
            "partition_started_at":   self._partition_started_at,
            "partition_ended_at":     self._partition_ended_at,
            "minority_read_only":     self._minority_read_only,
        }


def get_partition_state() -> PartitionState:
    return PartitionState.instance()


# ── Background loop ─────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _detector_loop() -> None:
    global _running
    _running = True
    logger.info("partition_detector_started", interval_sec=CHECK_INTERVAL_SEC)
    try:
        while _running:
            try:
                await _check_once()
            except Exception as e:
                logger.warning("partition_check_failed", error=str(e))
            await asyncio.sleep(CHECK_INTERVAL_SEC)
    finally:
        logger.info("partition_detector_stopped")


async def _check_once() -> None:
    from app.services.node_registry import get_registry
    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False)
             if not n.self_node and n.is_fresh()]
    get_partition_state().update(len(peers))


def start_partition_detector() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(
            _detector_loop(),
            name="partition-detector",
        )
    except RuntimeError:
        logger.warning("partition_detector_no_event_loop_yet")


def stop_partition_detector() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
