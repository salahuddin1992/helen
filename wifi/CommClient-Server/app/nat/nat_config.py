"""NAT-package configuration — env-tunable defaults."""

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
class NATConfig:
    # Detection
    stun_server:      str = field(default_factory=lambda: _s("HELEN_NAT_STUN", ""))
    stun_port:        int = field(default_factory=lambda: _i("HELEN_NAT_STUN_PORT", 3478))
    detect_timeout_sec: float = field(default_factory=lambda: _f("HELEN_NAT_DETECT_TIMEOUT", 3.0))
    redetect_interval_sec: float = field(default_factory=lambda: _f("HELEN_NAT_REDETECT_SEC", 600.0))

    # Strategy ladder
    enable_udp_punch: bool = field(default_factory=lambda: _b("HELEN_NAT_UDP_PUNCH", True))
    enable_tcp_punch: bool = field(default_factory=lambda: _b("HELEN_NAT_TCP_PUNCH", True))
    enable_reverse_tunnel: bool = field(default_factory=lambda: _b("HELEN_NAT_TUNNEL", True))
    enable_relay_fallback: bool = field(default_factory=lambda: _b("HELEN_NAT_RELAY", True))

    # Rendezvous
    rendezvous_host: str = field(default_factory=lambda: _s("HELEN_RENDEZVOUS_HOST", ""))
    rendezvous_port: int = field(default_factory=lambda: _i("HELEN_RENDEZVOUS_PORT", 4242))

    # Hole punching
    punch_attempts:   int = field(default_factory=lambda: _i("HELEN_NAT_PUNCH_ATTEMPTS", 5))
    punch_timeout_sec: float = field(default_factory=lambda: _f("HELEN_NAT_PUNCH_TIMEOUT", 3.0))
    punch_packet_count: int = field(default_factory=lambda: _i("HELEN_NAT_PUNCH_PACKETS", 8))

    # Sessions
    session_ttl_sec:  float = field(default_factory=lambda: _f("HELEN_NAT_SESSION_TTL", 300.0))
    session_max:      int = field(default_factory=lambda: _i("HELEN_NAT_SESSION_MAX", 256))


_singleton: NATConfig | None = None


def get_config() -> NATConfig:
    global _singleton
    if _singleton is None:
        _singleton = NATConfig()
    return _singleton


def reload_config() -> NATConfig:
    global _singleton
    _singleton = NATConfig()
    return _singleton
