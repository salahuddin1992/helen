"""Consistency manager — caller-selectable consistency levels.

Helen-Mesh is *eventually consistent* by default (gossip + LWW). For
operations that need stronger guarantees the caller can request:

  * ``EVENTUAL``    — fire-and-forget, fastest
  * ``READ_LOCAL``  — read from local replica only
  * ``READ_QUORUM`` — read with K-acks for read-your-writes
  * ``WRITE_QUORUM`` — quorum write before returning success
  * ``LINEAR``      — quorum write + quorum read (ordered)

The manager maps each level to the right combination of services
(replication / quorum / consensus) so application code can stay
declarative.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from app.distributed_system.consensus_manager import get_consensus_manager
from app.distributed_system.distributed_exceptions import ConsistencyError
from app.distributed_system.replication_manager import (
    get_replicated, put_replicated,
)


class ConsistencyLevel(str, Enum):
    EVENTUAL     = "eventual"
    READ_LOCAL   = "read_local"
    READ_QUORUM  = "read_quorum"
    WRITE_QUORUM = "write_quorum"
    LINEAR       = "linear"


async def write(
    kind: str, key: str, value: Any,
    *,
    level: ConsistencyLevel = ConsistencyLevel.EVENTUAL,
) -> dict:
    if level is ConsistencyLevel.EVENTUAL:
        return put_replicated(kind, key, value)
    if level in (ConsistencyLevel.WRITE_QUORUM, ConsistencyLevel.LINEAR):
        return await get_consensus_manager().write(kind, key, value)
    if level in (ConsistencyLevel.READ_LOCAL, ConsistencyLevel.READ_QUORUM):
        raise ConsistencyError(
            f"{level.value} is a read level, not write")
    raise ConsistencyError(f"unknown consistency level {level!r}")


async def read(
    kind: str, key: str,
    *,
    level: ConsistencyLevel = ConsistencyLevel.READ_LOCAL,
) -> Optional[dict]:
    if level is ConsistencyLevel.READ_LOCAL:
        return get_replicated(kind, key)
    if level in (ConsistencyLevel.READ_QUORUM, ConsistencyLevel.LINEAR):
        return await get_consensus_manager().read(kind, key)
    if level in (ConsistencyLevel.EVENTUAL, ConsistencyLevel.WRITE_QUORUM):
        return get_replicated(kind, key)
    raise ConsistencyError(f"unknown consistency level {level!r}")
