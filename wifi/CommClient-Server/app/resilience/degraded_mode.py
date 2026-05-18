"""Degraded-mode flag — coarse cluster health summary.

Three levels:

    NORMAL    — full functionality.
    DEGRADED  — non-essential operations rejected (logs, analytics);
                core flows still work.
    EMERGENCY — only health checks + admin endpoints accepted.

Level is recomputed every ``degraded_check_sec`` from inputs:

  * partition_detector minority?
  * backpressure REJECTED?
  * phi suspect rate over a threshold?
  * recent retry exhaustions over a threshold?

Callers ask ``decide(essential=True/False) -> (allow, level)``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from enum import Enum
from typing import Optional

from app.core.logging import get_logger
from app.resilience.resilience_config import get_config
from app.resilience.resilience_events import emit
from app.resilience.resilience_exceptions import DegradedModeBlockedError

logger = get_logger(__name__)


class DegradedLevel(str, Enum):
    NORMAL    = "normal"
    DEGRADED  = "degraded"
    EMERGENCY = "emergency"


class DegradedMode:
    _singleton: "DegradedMode | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._level: DegradedLevel = DegradedLevel.NORMAL
        self._inputs: dict = {}
        self._last_change_at: float = 0.0
        self._last_tick_at: float = 0.0
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "DegradedMode":
        if cls._singleton is None:
            cls._singleton = DegradedMode()
        return cls._singleton

    # ── Inputs ────────────────────────────────────────────

    @staticmethod
    def _gather_inputs() -> dict:
        out: dict = {}
        try:
            from app.services.partition_detector import get_partition_state
            out["majority"] = bool(get_partition_state().is_majority())
        except Exception:
            out["majority"] = True
        try:
            from app.services.backpressure import get_backpressure
            out["backpressure"] = get_backpressure().snapshot().get("level", "normal")
        except Exception:
            out["backpressure"] = "normal"
        try:
            from app.services.phi_accrual import get_phi_registry
            snap = get_phi_registry().snapshot()
            peers = snap.get("peers", {})
            ths = float(snap.get("threshold", 8.0))
            suspect = sum(1 for p in peers.values()
                          if p.get("phi", 0) >= ths)
            total = max(1, len(peers))
            out["suspect_rate"] = round(suspect / total, 3)
        except Exception:
            out["suspect_rate"] = 0.0
        return out

    # ── Decision ──────────────────────────────────────────

    def tick(self) -> DegradedLevel:
        inputs = self._gather_inputs()
        new_level = DegradedLevel.NORMAL
        if not inputs.get("majority", True):
            new_level = DegradedLevel.EMERGENCY
        elif inputs.get("backpressure") == "rejected":
            new_level = DegradedLevel.DEGRADED
        elif inputs.get("suspect_rate", 0.0) >= 0.5:
            new_level = DegradedLevel.DEGRADED
        with self._lock:
            old = self._level
            self._level = new_level
            self._inputs = inputs
            self._last_tick_at = time.time()
            if old is not new_level:
                self._last_change_at = self._last_tick_at
        if old is not new_level:
            emit("degraded.level_changed", {
                "old": old.value, "new": new_level.value,
                "inputs": inputs,
            })
        return new_level

    def level(self) -> DegradedLevel:
        with self._lock:
            return self._level

    def decide(self, *, essential: bool = False) -> tuple[bool, DegradedLevel]:
        with self._lock:
            level = self._level
        if essential:
            return True, level
        if level is DegradedLevel.NORMAL:
            return True, level
        if level is DegradedLevel.DEGRADED:
            return False, level
        # EMERGENCY → reject anything non-essential.
        return False, level

    def require(self, *, essential: bool = False) -> None:
        ok, level = self.decide(essential=essential)
        if not ok:
            raise DegradedModeBlockedError(level.value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "level":            self._level.value,
                "inputs":           dict(self._inputs),
                "last_change_at":   self._last_change_at,
                "last_tick_at":     self._last_tick_at,
            }

    # ── Background loop ───────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("res_degraded_started",
                    interval_sec=cfg.degraded_check_sec)
        try:
            while self._running:
                try:
                    self.tick()
                except Exception as e:
                    logger.warning("res_degraded_tick_failed", error=str(e))
                await asyncio.sleep(cfg.degraded_check_sec)
        finally:
            logger.info("res_degraded_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="resilience-degraded",
            )
        except RuntimeError:
            logger.warning("res_degraded_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_degraded_mode() -> DegradedMode:
    return DegradedMode.instance()
