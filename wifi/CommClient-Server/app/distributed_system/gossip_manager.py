"""Gossip manager — facade over services.anti_entropy.

Distributed-system callers ask for a gossip cycle via this module.
The lower-level loop (anti_entropy) runs on its own schedule; this
facade just exposes ``trigger_now()`` and stats.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.distributed_system.distributed_events import emit
from app.distributed_system.distributed_exceptions import GossipError

logger = get_logger(__name__)


async def trigger_now() -> dict:
    """Run one anti-entropy cycle on demand."""
    try:
        from app.services.anti_entropy import _cycle as ae_cycle
    except ImportError as e:
        raise GossipError(f"anti_entropy missing: {e}")
    try:
        await ae_cycle()
    except Exception as e:
        raise GossipError(str(e))
    payload = {"triggered": True}
    emit("gossip.cycle", payload)
    return payload


def local_state_hash() -> dict:
    try:
        from app.services.anti_entropy import local_trust_hashes
        h = local_trust_hashes()
        return {"trust_hashes_count": len(h)}
    except Exception:
        return {}
