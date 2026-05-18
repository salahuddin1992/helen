"""Shard manager — facade over services.consistent_hash.

Resolves "who owns this key?" using the consistent-hash ring of
the live cluster. Used by task_distributor to pick the unique node
that should perform a given piece of work.
"""

from __future__ import annotations

from typing import Optional

from app.distributed_system.distributed_exceptions import ShardError


def owner_of(key: str) -> Optional[str]:
    try:
        from app.services.consistent_hash import (
            get_ring, refresh_from_registry,
        )
    except ImportError as e:
        raise ShardError(f"consistent_hash missing: {e}")
    refresh_from_registry()
    return get_ring().owner_of(key)


def replicas_for(key: str, k: int = 3) -> list[str]:
    try:
        from app.services.consistent_hash import (
            get_ring, refresh_from_registry,
        )
    except ImportError as e:
        raise ShardError(f"consistent_hash missing: {e}")
    refresh_from_registry()
    return get_ring().replicas_for(key, k=k)


def i_am_owner(key: str) -> bool:
    from app.distributed_system.node_identity import server_id
    return owner_of(key) == server_id()


def keyspace_share(sample_keys: int = 5_000) -> dict[str, float]:
    try:
        from app.services.consistent_hash import (
            get_ring, refresh_from_registry,
        )
    except ImportError as e:
        raise ShardError(f"consistent_hash missing: {e}")
    refresh_from_registry()
    return get_ring().keyspace_share(sample_keys=sample_keys)
