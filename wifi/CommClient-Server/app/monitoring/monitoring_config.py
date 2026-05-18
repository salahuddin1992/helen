"""Monitoring-package configuration — env-tunable defaults."""

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
class MonitoringConfig:
    health_check_interval_sec: float = field(
        default_factory=lambda: _f("HELEN_MON_HEALTH_SEC", 15.0))
    metrics_collect_interval_sec: float = field(
        default_factory=lambda: _f("HELEN_MON_METRICS_SEC", 10.0))
    snapshot_interval_sec: float = field(
        default_factory=lambda: _f("HELEN_MON_SNAPSHOT_SEC", 60.0))
    alert_check_interval_sec: float = field(
        default_factory=lambda: _f("HELEN_MON_ALERT_SEC", 30.0))

    # Latency tracker
    latency_window: int = field(
        default_factory=lambda: _i("HELEN_MON_LAT_WINDOW", 500))

    # History caps
    health_history_max: int = field(
        default_factory=lambda: _i("HELEN_MON_HEALTH_HISTORY", 100))
    snapshot_history_max: int = field(
        default_factory=lambda: _i("HELEN_MON_SNAP_HISTORY", 50))

    # Feature flags
    enable_alerts:    bool = field(
        default_factory=lambda: _b("HELEN_MON_ALERTS", True))
    enable_snapshots: bool = field(
        default_factory=lambda: _b("HELEN_MON_SNAPSHOTS", True))


_singleton: MonitoringConfig | None = None


def get_config() -> MonitoringConfig:
    global _singleton
    if _singleton is None:
        _singleton = MonitoringConfig()
    return _singleton


def reload_config() -> MonitoringConfig:
    global _singleton
    _singleton = MonitoringConfig()
    return _singleton
