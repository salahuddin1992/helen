"""Leader election — facade over services.distributed_lock.

A "leader" in Helen is whoever currently holds the named cluster
lock. There's no leader for the whole cluster (we don't need one) —
only per-task leaders for singleton work like log compaction or
backup uploads.

This file exposes the leader concept *as if* it were the standard
Raft leader API so application code reads naturally even when the
underlying primitive is sloppy-quorum.
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator

from app.distributed_system.distributed_config import get_config
from app.distributed_system.distributed_events import emit
from app.distributed_system.distributed_exceptions import LeaderElectionError


@contextlib.asynccontextmanager
async def lead(name: str, *,
               ttl_sec: float | None = None,
               acquire_timeout_sec: float = 5.0,
               poll_interval_sec: float = 1.0) -> AsyncIterator[bool]:
    """Async context manager — yields True iff this node is the leader
    of ``name`` for the duration of the block. The lease auto-renews
    in the background while inside the block.

    Usage::

        async with lead("audit_compactor") as is_leader:
            if not is_leader:
                return
            await do_singleton_work()
    """
    cfg = get_config()
    ttl = ttl_sec if ttl_sec is not None else cfg.leader_lease_ttl_sec
    try:
        from app.services.distributed_lock import distributed_lock
    except ImportError:
        raise LeaderElectionError("distributed_lock primitive missing")

    async with distributed_lock(
        name, ttl=ttl,
        acquire_timeout=acquire_timeout_sec,
        poll_interval=poll_interval_sec,
    ) as held:
        if held:
            emit("leader.acquired", {"name": name, "ttl": ttl})
        try:
            yield held
        finally:
            if held:
                emit("leader.released", {"name": name})


def status(name: str) -> dict:
    """Inspect the current leader of ``name``."""
    try:
        from app.services.distributed_lock import lock_status
        return lock_status(name)
    except Exception as e:
        return {"name": name, "error": str(e)}
