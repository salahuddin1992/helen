"""State sync facade — wraps services.state_reconciliation.

Distributed-system callers ask for a state-sync run via this module
instead of importing the underlying reconciliation loop directly.
"""

from __future__ import annotations

from typing import Optional

from app.core.logging import get_logger
from app.distributed_system.distributed_events import emit

logger = get_logger(__name__)


async def sync_now() -> Optional[dict]:
    """Trigger one reconciliation cycle on demand. Returns the local
    state hash before+after for diagnostic comparison."""
    try:
        from app.services.state_reconciliation import (
            compute_local_state_hash, _reconcile_once,
        )
    except ImportError as e:
        logger.warning("state_sync_primitive_missing", error=str(e))
        return None
    before = compute_local_state_hash()
    await _reconcile_once()
    after = compute_local_state_hash()
    payload = {
        "before_root": before.get("root"),
        "after_root":  after.get("root"),
        "changed":     before.get("root") != after.get("root"),
    }
    emit("state.synced", payload)
    return payload


def hash_snapshot() -> dict:
    """Local Merkle hashes — used by peers comparing convergence."""
    try:
        from app.services.state_reconciliation import compute_local_state_hash
        return compute_local_state_hash()
    except Exception:
        return {}
