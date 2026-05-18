"""Overlay-package configuration — env-tunable defaults."""

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
class OverlayConfig:
    refresh_interval_sec: float = field(
        default_factory=lambda: _f("HELEN_OVL_REFRESH_SEC", 30.0))
    max_route_hops:       int = field(
        default_factory=lambda: _i("HELEN_OVL_MAX_HOPS", 6))
    session_ttl_sec:      float = field(
        default_factory=lambda: _f("HELEN_OVL_SESSION_TTL_SEC", 600.0))
    max_overlays:         int = field(
        default_factory=lambda: _i("HELEN_OVL_MAX_OVERLAYS", 16))
    enable_persistence:   bool = field(
        default_factory=lambda: _b("HELEN_OVL_PERSIST", True))


_singleton: OverlayConfig | None = None


def get_config() -> OverlayConfig:
    global _singleton
    if _singleton is None:
        _singleton = OverlayConfig()
    return _singleton


def reload_config() -> OverlayConfig:
    global _singleton
    _singleton = OverlayConfig()
    return _singleton
