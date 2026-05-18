"""Distributed-system configuration — env-tunable defaults.

Every constant reads ``HELEN_DS_*`` env vars at instantiation so a
process restart picks up the change. Use ``reload_config`` in tests
to re-read after monkeypatching env.
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
class DistributedConfig:
    # Membership
    membership_check_sec: float = field(default_factory=lambda: _f("HELEN_DS_MEMBER_CHECK_SEC", 10.0))
    member_stale_sec:     float = field(default_factory=lambda: _f("HELEN_DS_MEMBER_STALE_SEC", 15.0))
    member_dead_sec:      float = field(default_factory=lambda: _f("HELEN_DS_MEMBER_DEAD_SEC", 45.0))

    # Heartbeat
    heartbeat_interval_sec: float = field(default_factory=lambda: _f("HELEN_DS_HB_SEC", 5.0))

    # Leader election
    leader_lease_ttl_sec: float = field(default_factory=lambda: _f("HELEN_DS_LEADER_TTL_SEC", 60.0))

    # Consensus / replication
    replication_factor:   int   = field(default_factory=lambda: _i("HELEN_DS_REPLICATION", 3))
    quorum_timeout_sec:   float = field(default_factory=lambda: _f("HELEN_DS_QUORUM_TIMEOUT", 4.0))

    # State sync
    state_sync_sec:       float = field(default_factory=lambda: _f("HELEN_DS_STATE_SYNC_SEC", 60.0))

    # Recovery
    recovery_check_sec:   float = field(default_factory=lambda: _f("HELEN_DS_RECOVERY_SEC", 30.0))

    # Gossip
    gossip_interval_sec:  float = field(default_factory=lambda: _f("HELEN_DS_GOSSIP_SEC", 5.0))
    gossip_fanout:        int   = field(default_factory=lambda: _i("HELEN_DS_GOSSIP_K", 10))

    # Feature flags
    enable_auto_recovery: bool = field(default_factory=lambda: _b("HELEN_DS_AUTO_RECOVERY", True))
    enable_consensus:     bool = field(default_factory=lambda: _b("HELEN_DS_CONSENSUS", True))


_singleton: DistributedConfig | None = None


def get_config() -> DistributedConfig:
    global _singleton
    if _singleton is None:
        _singleton = DistributedConfig()
    return _singleton


def reload_config() -> DistributedConfig:
    global _singleton
    _singleton = DistributedConfig()
    return _singleton
