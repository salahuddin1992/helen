"""
Circuit breaker — per-target failure tracking with three-state
machine (closed → open → half-open).

Today's implementation lives inside ``federation_service._PeerBreaker``
and is per-peer-only. ``route_planner``, ``broker_client``, and
``object_storage_service`` need the same primitive against different
targets (peer servers, NATS subjects, S3 buckets). Hoisting it out
gives every caller the same retry semantics and observable state.

State machine
-------------
::

    closed        — calls pass through; failures counted
       │
       │ N consecutive failures (default 3)
       ▼
    open          — calls short-circuit immediately with CircuitOpenError
       │
       │ cooldown elapses (default 30s)
       ▼
    half-open     — single probe call allowed
       │
       │ success                                  failure
       ▼                                          ▼
    closed                                     open (reset cooldown)

API
---
    >>> cb = CircuitBreakerService()
    >>> async def expensive_call(): ...
    >>> result = await cb.call("peer_037", expensive_call)
    >>> # raises CircuitOpenError if "peer_037" is currently open
    >>> state = cb.state("peer_037")  # "closed" | "open" | "half_open"
    >>> cb.record_failure("peer_037")  # manual record (e.g. timeout)
    >>> cb.record_success("peer_037")
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, TypeVar, Literal

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")
State = Literal["closed", "open", "half_open"]


class CircuitOpenError(Exception):
    """Raised when ``call()`` is invoked on an open breaker."""

    def __init__(self, target: str, opened_at: float, cooldown_sec: float):
        self.target = target
        self.opened_at = opened_at
        self.cooldown_sec = cooldown_sec
        super().__init__(
            f"circuit OPEN for {target} "
            f"(remaining cooldown: {max(0.0, cooldown_sec - (time.time() - opened_at)):.1f}s)"
        )


@dataclass
class _Stats:
    state: State = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0
    last_failure_at: float = 0.0
    half_open_in_flight: bool = False
    total_failures: int = 0
    total_successes: int = 0


class CircuitBreakerService:
    """Generic per-target circuit breaker. Keys are arbitrary strings —
    use ``peer:server_037`` for peers, ``nats:subject.foo`` for broker
    subjects, ``s3:bucket-name`` for object storage, etc."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        half_open_probe_timeout_sec: float = 10.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_probe_timeout = half_open_probe_timeout_sec
        self._stats: dict[str, _Stats] = {}
        self._lock = asyncio.Lock()

    # ── Inspection ─────────────────────────────────────────────

    def state(self, target: str) -> State:
        s = self._stats.get(target)
        if s is None:
            return "closed"
        # Lazy state advance: if we're open and cooldown elapsed,
        # advance to half-open on next access (no background task
        # needed). Half-open lets exactly one probe through.
        if s.state == "open" and (time.time() - s.opened_at) >= self.cooldown_seconds:
            s.state = "half_open"
            s.half_open_in_flight = False
        return s.state

    def stats(self, target: str) -> _Stats:
        return self._stats.get(target, _Stats())

    # ── Mutation ───────────────────────────────────────────────

    def record_success(self, target: str) -> None:
        s = self._stats.get(target)
        if s is None:
            self._stats[target] = _Stats(total_successes=1)
            return
        s.total_successes += 1
        s.consecutive_failures = 0
        if s.state in ("open", "half_open"):
            logger.info(
                "circuit_breaker_closed",
                target=target,
                successes=s.total_successes,
            )
        s.state = "closed"
        s.opened_at = 0.0
        s.half_open_in_flight = False

    def record_failure(self, target: str) -> None:
        s = self._stats.setdefault(target, _Stats())
        s.total_failures += 1
        s.consecutive_failures += 1
        s.last_failure_at = time.time()
        if s.state == "half_open":
            # The probe failed — back to open, reset cooldown.
            s.state = "open"
            s.opened_at = time.time()
            s.half_open_in_flight = False
            logger.warning(
                "circuit_breaker_reopened",
                target=target,
                failures=s.consecutive_failures,
            )
            return
        if s.state == "closed" and s.consecutive_failures >= self.failure_threshold:
            s.state = "open"
            s.opened_at = time.time()
            logger.warning(
                "circuit_breaker_opened",
                target=target,
                failures=s.consecutive_failures,
                cooldown_sec=self.cooldown_seconds,
            )

    def reset(self, target: str) -> None:
        """Force a target back to closed. Admin/recovery use only."""
        self._stats.pop(target, None)

    # ── Wrapped invocation ─────────────────────────────────────

    async def call(
        self,
        target: str,
        fn: Callable[[], Awaitable[T]],
    ) -> T:
        """Invoke ``fn()`` if the breaker permits. Records success/
        failure automatically. Raises ``CircuitOpenError`` if the
        breaker is open. In half-open, only the first concurrent
        caller is allowed through; the rest get CircuitOpenError
        until the probe resolves.

        Chaos hook
        ----------
        When the chaos admin endpoint has injected a non-zero failure
        rate for ``target``, we synthesize the failure BEFORE invoking
        ``fn()`` — at random with that probability. This lets chaos
        tests force breaker state transitions without needing the
        underlying transport to actually fail, and crucially exercises
        every retry / DLQ / route-recompute code path even on a
        healthy lab cluster.
        """
        # Chaos failure injection — best-effort lazy import so a circular
        # import at module load can never break this hot path.
        try:
            from app.api.routes.chaos import get_failure_rate as _gfr
            rate = _gfr(target)
            if rate > 0.0:
                import random as _r
                if _r.random() < rate:
                    self.record_failure(target)
                    raise RuntimeError(
                        f"chaos_injected_failure target={target} rate={rate}"
                    )
        except RuntimeError:
            raise
        except Exception:
            pass  # chaos module unavailable — continue normally

        st = self.state(target)
        if st == "open":
            s = self._stats[target]
            raise CircuitOpenError(target, s.opened_at, self.cooldown_seconds)

        if st == "half_open":
            async with self._lock:
                s = self._stats[target]
                if s.half_open_in_flight:
                    raise CircuitOpenError(target, s.opened_at, self.cooldown_seconds)
                s.half_open_in_flight = True

        try:
            try:
                result = await asyncio.wait_for(
                    fn(),
                    timeout=self.half_open_probe_timeout if st == "half_open" else None,
                )
            except asyncio.TimeoutError:
                self.record_failure(target)
                raise
            self.record_success(target)
            return result
        except Exception:
            self.record_failure(target)
            raise


# ── Module-level singleton ──────────────────────────────────────────
# Most callers just want a shared breaker. Specialty configs (longer
# cooldown for object storage, etc.) can construct their own.

_default: "CircuitBreakerService | None" = None


def get_default() -> CircuitBreakerService:
    global _default
    if _default is None:
        _default = CircuitBreakerService()
    return _default
