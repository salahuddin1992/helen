"""P2P-package configuration — env-tunable defaults."""

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
class P2PConfig:
    # Peer selection
    selection_top_k:        int = field(default_factory=lambda: _i("HELEN_P2P_SELECT_K", 8))
    selection_min_trust:    float = field(default_factory=lambda: _f("HELEN_P2P_MIN_TRUST", 0.10))
    selection_max_phi:      float = field(default_factory=lambda: _f("HELEN_P2P_MAX_PHI", 8.0))

    # Handshake
    handshake_timeout_sec:  float = field(default_factory=lambda: _f("HELEN_P2P_HS_TIMEOUT", 3.0))
    handshake_max_retries:  int = field(default_factory=lambda: _i("HELEN_P2P_HS_RETRIES", 2))

    # Connection
    connect_timeout_sec:    float = field(default_factory=lambda: _f("HELEN_P2P_CONN_TIMEOUT", 5.0))
    keep_alive_sec:         float = field(default_factory=lambda: _f("HELEN_P2P_KEEP_ALIVE", 30.0))

    # Forwarding
    max_forward_hops:       int = field(default_factory=lambda: _i("HELEN_P2P_MAX_HOPS", 4))
    forward_fanout:         int = field(default_factory=lambda: _i("HELEN_P2P_FORWARD_K", 8))

    # Lifecycle
    refresh_interval_sec:   float = field(default_factory=lambda: _f("HELEN_P2P_REFRESH_SEC", 30.0))

    # NAT traversal
    enable_hole_punch:      bool = field(default_factory=lambda: _b("HELEN_P2P_HOLE_PUNCH", True))
    enable_reverse_tunnel:  bool = field(default_factory=lambda: _b("HELEN_P2P_TUNNEL", True))


_singleton: P2PConfig | None = None


def get_config() -> P2PConfig:
    global _singleton
    if _singleton is None:
        _singleton = P2PConfig()
    return _singleton


def reload_config() -> P2PConfig:
    global _singleton
    _singleton = P2PConfig()
    return _singleton
