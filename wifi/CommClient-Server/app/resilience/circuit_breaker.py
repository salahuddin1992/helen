"""Per-target circuit breaker — closed/open/half-open.

State machine:

    CLOSED    — pass everything; record successes + failures.
    OPEN      — reject everything for ``open_sec`` seconds, then
                transition to HALF_OPEN.
    HALF_OPEN — allow ``half_open_probes`` test requests; on success
                go CLOSED, on failure go OPEN again.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from app.resilience.resilience_config import get_config
from app.resilience.resilience_events import emit
from app.resilience.resilience_exceptions import CircuitOpenError


class BreakerState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Breaker:
    target:               str
    state:                BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    open_until:           float = 0.0
    half_open_attempts:   int = 0


class CircuitBreakerRegistry:
    _singleton: "CircuitBreakerRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._breakers: dict[str, _Breaker] = {}

    @classmethod
    def instance(cls) -> "CircuitBreakerRegistry":
        if cls._singleton is None:
            cls._singleton = CircuitBreakerRegistry()
        return cls._singleton

    def _get(self, target: str) -> _Breaker:
        with self._lock:
            b = self._breakers.get(target)
            if b is None:
                b = _Breaker(target=target)
                self._breakers[target] = b
            return b

    # ── Decision ──────────────────────────────────────────

    def allow(self, target: str) -> bool:
        cfg = get_config()
        b = self._get(target)
        with self._lock:
            now = time.time()
            if b.state is BreakerState.OPEN and now >= b.open_until:
                b.state = BreakerState.HALF_OPEN
                b.half_open_attempts = 0
                emit("breaker.half_open", {"target": target})
            if b.state is BreakerState.OPEN:
                return False
            if b.state is BreakerState.HALF_OPEN:
                if b.half_open_attempts >= cfg.breaker_half_open_probes:
                    return False
                b.half_open_attempts += 1
            return True

    def require_allow(self, target: str) -> None:
        if not self.allow(target):
            raise CircuitOpenError(target)

    # ── Outcomes ──────────────────────────────────────────

    def record_success(self, target: str) -> None:
        b = self._get(target)
        with self._lock:
            old_state = b.state
            b.consecutive_failures = 0
            b.state = BreakerState.CLOSED
            b.half_open_attempts = 0
            b.open_until = 0.0
        if old_state is not BreakerState.CLOSED:
            emit("breaker.closed", {"target": target})

    def record_failure(self, target: str) -> None:
        cfg = get_config()
        b = self._get(target)
        with self._lock:
            b.consecutive_failures += 1
            old_state = b.state
            if b.consecutive_failures >= cfg.breaker_fail_count:
                b.state = BreakerState.OPEN
                b.open_until = time.time() + cfg.breaker_open_sec
                b.half_open_attempts = 0
            elif b.state is BreakerState.HALF_OPEN:
                b.state = BreakerState.OPEN
                b.open_until = time.time() + cfg.breaker_open_sec
        if b.state is BreakerState.OPEN and old_state is not BreakerState.OPEN:
            emit("breaker.open", {
                "target": target,
                "fails":  b.consecutive_failures,
                "open_until": b.open_until,
            })

    def reset(self, target: str) -> bool:
        with self._lock:
            existed = self._breakers.pop(target, None) is not None
        if existed:
            emit("breaker.reset", {"target": target})
        return existed

    def state(self, target: str) -> str:
        return self._get(target).state.value

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": len(self._breakers),
                "breakers": [
                    {
                        "target":               b.target,
                        "state":                b.state.value,
                        "consecutive_failures": b.consecutive_failures,
                        "open_until_in_sec":    max(0.0,
                                                    round(b.open_until - time.time(), 1))
                                                if b.open_until else 0.0,
                    }
                    for b in self._breakers.values()
                ],
            }


def get_breaker_registry() -> CircuitBreakerRegistry:
    return CircuitBreakerRegistry.instance()
