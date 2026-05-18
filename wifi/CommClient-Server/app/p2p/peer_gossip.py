"""Peer gossip facade — wraps services.anti_entropy."""

from __future__ import annotations

from app.p2p.peer_events import emit


async def trigger_gossip_cycle() -> dict:
    """Ask the anti-entropy loop to run once on demand."""
    try:
        from app.services.anti_entropy import _cycle as ae_cycle
        await ae_cycle()
        emit("gossip.cycle", {"triggered": True})
        return {"triggered": True}
    except Exception as e:
        emit("gossip.failed", {"error": str(e)[:80]})
        return {"triggered": False, "error": str(e)[:80]}


def known_state_hashes_count() -> int:
    try:
        from app.services.anti_entropy import local_trust_hashes
        return len(local_trust_hashes())
    except Exception:
        return 0


def gossip_snapshot() -> dict:
    return {"known_state_hashes_count": known_state_hashes_count()}
