"""Quorum manager — math + facade over services.quorum_decision.

Exposes:

  * ``required_acks(replication)`` — strict majority (⌈N/2⌉ + 1).
  * ``write(...)`` and ``read(...)`` shortcuts.
"""

from __future__ import annotations

import math
from typing import Any, Optional


def required_acks(replication: int, override: Optional[int] = None) -> int:
    if override is not None:
        return max(1, int(override))
    return math.floor(replication / 2) + 1


async def write(
    kind: str, key: str, value: Any,
    *, replication: int = 3, required: Optional[int] = None,
    timeout: float = 4.0,
) -> dict:
    try:
        from app.services.quorum_decision import quorum_write
    except ImportError as e:
        raise ImportError(f"quorum_decision missing: {e}")
    result = await quorum_write(
        kind=kind, key=key, value=value,
        replication=replication, required_acks=required,
        timeout=timeout,
    )
    return {
        "accepted":      result.accepted,
        "acks_received": result.acks_received,
        "acks_required": result.acks_required,
        "duration_ms":   result.duration_ms,
        "failures":      list(result.failures),
    }


async def read(
    kind: str, key: str,
    *, replication: int = 3, required: Optional[int] = None,
    timeout: float = 4.0,
) -> Optional[dict]:
    try:
        from app.services.quorum_decision import quorum_read
    except ImportError as e:
        raise ImportError(f"quorum_decision missing: {e}")
    return await quorum_read(
        kind=kind, key=key,
        replication=replication, required_acks=required, timeout=timeout,
    )
