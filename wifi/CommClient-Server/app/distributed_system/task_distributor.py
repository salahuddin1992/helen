"""Task distributor — assign work to the right node.

Combines:
  * shard_manager.owner_of  → who's responsible
  * leader_election.lead    → singleton lock for the work
  * consensus_manager       → record the assignment durably (optional)

Two assignment modes:

  * ``shard_assigned``  — partition the keyspace; consistent owner.
  * ``lease_assigned``  — first-come-first-served via leader_election.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from app.distributed_system.distributed_exceptions import TaskDistributionError
from app.distributed_system.leader_election import lead
from app.distributed_system.node_identity import server_id
from app.distributed_system.shard_manager import owner_of


def assigned_owner(task_key: str) -> str:
    """Deterministic owner for ``task_key`` via consistent hash."""
    o = owner_of(task_key)
    if not o:
        raise TaskDistributionError(f"no owner for task {task_key!r}")
    return o


def i_am_assigned(task_key: str) -> bool:
    return assigned_owner(task_key) == server_id()


@asynccontextmanager
async def lease_assigned(name: str, *,
                         ttl_sec: float = 300.0) -> AsyncIterator[bool]:
    """First-come-first-served task lease. Yields True if we hold it."""
    async with lead(name, ttl_sec=ttl_sec) as held:
        yield held


@asynccontextmanager
async def run_if_owner(task_key: str) -> AsyncIterator[bool]:
    """Run the body only if we're the consistent-hash owner of
    ``task_key``."""
    yield i_am_assigned(task_key)
