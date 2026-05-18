"""Resilience-package configuration — env-tunable defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _b(env: str, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ResilienceConfig:
    # Failure detector
    phi_threshold:        float = field(default_factory=lambda: _f("HELEN_RES_PHI", 8.0))

    # Circuit breaker
    breaker_fail_count:   int = field(default_factory=lambda: _i("HELEN_RES_BREAKER_FAILS", 5))
    breaker_open_sec:     float = field(default_factory=lambda: _f("HELEN_RES_BREAKER_OPEN_SEC", 30.0))
    breaker_half_open_probes: int = field(default_factory=lambda: _i("HELEN_RES_BREAKER_HALF_PROBES", 1))

    # Retry policy
    retry_max_attempts:   int = field(default_factory=lambda: _i("HELEN_RES_RETRY_MAX", 6))
    retry_base_sec:       float = field(default_factory=lambda: _f("HELEN_RES_RETRY_BASE_SEC", 1.0))
    retry_cap_sec:        float = field(default_factory=lambda: _f("HELEN_RES_RETRY_CAP_SEC", 60.0))
    retry_jitter_pct:     float = field(default_factory=lambda: _f("HELEN_RES_RETRY_JITTER", 0.20))

    # Retry queue
    retry_queue_ttl_sec:  float = field(default_factory=lambda: _f("HELEN_RES_QUEUE_TTL_SEC", 1800.0))
    retry_queue_max:      int = field(default_factory=lambda: _i("HELEN_RES_QUEUE_MAX", 1000))

    # Recovery loop
    recovery_check_sec:   float = field(default_factory=lambda: _f("HELEN_RES_RECOVERY_SEC", 30.0))

    # Degraded mode
    degraded_check_sec:   float = field(default_factory=lambda: _f("HELEN_RES_DEGRADED_SEC", 10.0))

    # Feature flags
    enable_auto_recovery: bool = field(default_factory=lambda: _b("HELEN_RES_AUTO_RECOVERY", True))


_singleton: ResilienceConfig | None = None


def get_config() -> ResilienceConfig:
    global _singleton
    if _singleton is None:
        _singleton = ResilienceConfig()
    return _singleton


def reload_config() -> ResilienceConfig:
    global _singleton
    _singleton = ResilienceConfig()
    return _singleton
