"""Routing-strategy configuration — defaults + env overrides.

Each tunable lives here so operators can adjust behaviour without
editing strategy implementations. Every constant reads
``HELEN_RS_*`` env vars at import time so a process restart picks
up the change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key) or default)
    except (TypeError, ValueError):
        return default


def _i(env_key: str, default: int) -> int:
    try:
        return int(os.environ.get(env_key) or default)
    except (TypeError, ValueError):
        return default


def _b(env_key: str, default: bool) -> bool:
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class StrategyConfig:
    """Each ``__init__`` re-reads env so ``reload_config`` picks up
    runtime changes (used by tests + ops hot-tuning)."""
    # Selection
    top_k:                 int = field(default_factory=lambda: _i("HELEN_RS_TOP_K", 4))
    fanout_parallel:       int = field(default_factory=lambda: _i("HELEN_RS_PARALLEL", 1))
    refresh_interval_sec:  float = field(default_factory=lambda: _f("HELEN_RS_REFRESH_SEC", 30.0))

    # Scoring weights (summed normalised at score time)
    w_latency:    float = field(default_factory=lambda: _f("HELEN_RS_W_LATENCY", 0.25))
    w_loss:       float = field(default_factory=lambda: _f("HELEN_RS_W_LOSS",     0.15))
    w_bandwidth:  float = field(default_factory=lambda: _f("HELEN_RS_W_BW",       0.10))
    w_trust:      float = field(default_factory=lambda: _f("HELEN_RS_W_TRUST",    0.15))
    w_load:       float = field(default_factory=lambda: _f("HELEN_RS_W_LOAD",     0.10))
    w_hops:       float = field(default_factory=lambda: _f("HELEN_RS_W_HOPS",     0.10))
    w_age:        float = field(default_factory=lambda: _f("HELEN_RS_W_AGE",      0.05))
    w_security:   float = field(default_factory=lambda: _f("HELEN_RS_W_SECURITY", 0.05))
    w_nat:        float = field(default_factory=lambda: _f("HELEN_RS_W_NAT",      0.05))

    # Hard rejection thresholds
    trust_floor:  float = field(default_factory=lambda: _f("HELEN_RS_TRUST_FLOOR", 0.10))
    phi_ceiling:  float = field(default_factory=lambda: _f("HELEN_RS_PHI_CEILING", 8.0))

    # Failover
    cooldown_sec: float = field(default_factory=lambda: _f("HELEN_RS_COOLDOWN_SEC", 30.0))
    max_attempts: int   = field(default_factory=lambda: _i("HELEN_RS_MAX_ATTEMPTS", 3))

    # NAT-aware
    prefer_open_nat: bool = field(default_factory=lambda: _b("HELEN_RS_PREFER_OPEN_NAT", True))

    # Trust-aware
    require_trusted_first_hop: bool = field(default_factory=lambda: _b("HELEN_RS_REQUIRE_TRUSTED_HOP", True))

    # Adaptive — meta-strategy switching
    adaptive_enabled: bool = field(default_factory=lambda: _b("HELEN_RS_ADAPTIVE", True))


_singleton: StrategyConfig | None = None


def get_config() -> StrategyConfig:
    global _singleton
    if _singleton is None:
        _singleton = StrategyConfig()
    return _singleton


def reload_config() -> StrategyConfig:
    """Force a fresh read of env vars — useful in tests."""
    global _singleton
    _singleton = StrategyConfig()
    return _singleton
