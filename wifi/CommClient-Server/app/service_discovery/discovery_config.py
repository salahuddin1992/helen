"""Service-discovery package configuration — env-tunable defaults."""

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


def _s(env: str, default: str) -> str:
    return os.environ.get(env, default) or default


@dataclass(frozen=True)
class DiscoveryConfig:
    # TTL + heartbeat
    default_ttl_sec:        float = field(default_factory=lambda: _f("HELEN_SD_TTL_SEC", 60.0))
    heartbeat_grace_sec:    float = field(default_factory=lambda: _f("HELEN_SD_GRACE_SEC", 15.0))
    reaper_interval_sec:    float = field(default_factory=lambda: _f("HELEN_SD_REAPER_SEC", 10.0))

    # Latency probing
    probe_interval_sec:     float = field(default_factory=lambda: _f("HELEN_SD_PROBE_SEC", 30.0))
    probe_timeout_sec:      float = field(default_factory=lambda: _f("HELEN_SD_PROBE_TIMEOUT", 2.0))
    probe_fanout:           int   = field(default_factory=lambda: _i("HELEN_SD_PROBE_FANOUT", 20))

    # Selection
    min_health_score:       float = field(default_factory=lambda: _f("HELEN_SD_MIN_HEALTH", 0.30))
    capacity_floor_pct:     float = field(default_factory=lambda: _f("HELEN_SD_CAPACITY_FLOOR_PCT", 5.0))

    # Region preferences
    same_region_bonus:      float = field(default_factory=lambda: _f("HELEN_SD_SAME_REGION_BONUS", 0.30))
    same_zone_bonus:        float = field(default_factory=lambda: _f("HELEN_SD_SAME_ZONE_BONUS", 0.10))

    # Region / zone of this node
    self_region:            str   = field(default_factory=lambda: _s("HELEN_SD_REGION", "default"))
    self_zone:              str   = field(default_factory=lambda: _s("HELEN_SD_ZONE", "default"))

    # Federation
    enable_federation_lookup: bool = field(default_factory=lambda: _b("HELEN_SD_FEDERATION", True))

    # Persistence
    persist_to_disk:        bool  = field(default_factory=lambda: _b("HELEN_SD_PERSIST", True))


_singleton: DiscoveryConfig | None = None


def get_config() -> DiscoveryConfig:
    global _singleton
    if _singleton is None:
        _singleton = DiscoveryConfig()
    return _singleton


def reload_config() -> DiscoveryConfig:
    global _singleton
    _singleton = DiscoveryConfig()
    return _singleton
