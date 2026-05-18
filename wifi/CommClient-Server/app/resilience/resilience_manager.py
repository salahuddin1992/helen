"""ResilienceManager — top-level start/stop + diagnostics aggregator.

Composes:

  * RetryQueue background dispatcher
  * RecoveryManager event subscriber + watchdog
  * DegradedMode periodic re-evaluation

All other classes (FailureDetector, FailureClassifier, RetryPolicy,
CircuitBreaker, Failover) are pure / on-demand and need no loop.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.resilience.circuit_breaker import get_breaker_registry
from app.resilience.degraded_mode import get_degraded_mode
from app.resilience.failure_detector import get_failure_detector
from app.resilience.recovery_manager import get_recovery_manager
from app.resilience.resilience_events import emit, history
from app.resilience.retry_queue import get_retry_queue
from app.resilience.retry_policy import policy_snapshot

logger = get_logger(__name__)


class ResilienceManager:
    _singleton: "ResilienceManager | None" = None

    def __init__(self) -> None:
        self._started = False

    @classmethod
    def instance(cls) -> "ResilienceManager":
        if cls._singleton is None:
            cls._singleton = ResilienceManager()
        return cls._singleton

    def start(self) -> None:
        if self._started:
            return
        get_retry_queue().start()
        get_recovery_manager().start()
        get_degraded_mode().start()
        self._started = True
        emit("resilience.started", {})
        logger.info("resilience_manager_started")

    def stop(self) -> None:
        if not self._started:
            return
        get_degraded_mode().stop()
        get_recovery_manager().stop()
        get_retry_queue().stop()
        self._started = False
        emit("resilience.stopped", {})
        logger.info("resilience_manager_stopped")

    def snapshot(self) -> dict:
        return {
            "started":       self._started,
            "degraded":      get_degraded_mode().snapshot(),
            "breaker":       get_breaker_registry().snapshot(),
            "retry_queue":   get_retry_queue().snapshot(),
            "retry_policy":  policy_snapshot(),
            "failure_detector": get_failure_detector().snapshot(),
            "recovery":      get_recovery_manager().stats(),
            "events":        history(limit=50),
        }


def get_resilience_manager() -> ResilienceManager:
    return ResilienceManager.instance()


def start_resilience() -> None:
    get_resilience_manager().start()


def stop_resilience() -> None:
    get_resilience_manager().stop()
