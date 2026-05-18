"""Partition detector facade.

Wraps services.partition_detector.PartitionState so distributed-
system callers stay in their own package.
"""

from __future__ import annotations

from app.distributed_system.distributed_exceptions import (
    PartitionDetectedError,
)


def is_majority() -> bool:
    try:
        from app.services.partition_detector import get_partition_state
        return get_partition_state().is_majority()
    except Exception:
        return True  # err on the side of accepting work


def is_read_only() -> bool:
    try:
        from app.services.partition_detector import get_partition_state
        return get_partition_state().is_read_only()
    except Exception:
        return False


def snapshot() -> dict:
    try:
        from app.services.partition_detector import get_partition_state
        return get_partition_state().snapshot()
    except Exception:
        return {}


def require_majority() -> None:
    """Raise PartitionDetectedError if we're in minority — useful in
    code paths that must not run during a partition."""
    if not is_majority():
        raise PartitionDetectedError("local node is not in majority partition")
