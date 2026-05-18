"""Per-peer failover — try a peer; on failure, escalate to the next.

A thin orchestrator over ``peer_selection`` that turns "send X to
peer Y" into a resilient multi-attempt operation.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from app.p2p.peer_events import emit
from app.p2p.peer_model import Peer
from app.p2p.peer_selection import select_for_relay


# Caller supplies an attempt function: (peer) -> awaitable[bool].
AttemptFn = Callable[[Peer], Awaitable[bool]]


async def try_with_failover(
    attempt: AttemptFn,
    *,
    max_attempts: int = 3,
    candidates: list[Peer] | None = None,
) -> tuple[bool, Peer | None, list[str]]:
    """Try ``attempt`` against the top relay candidates until one
    returns True. Returns (ok, peer_used, attempted_peer_ids).
    """
    if candidates is None:
        candidates = select_for_relay()
    attempted: list[str] = []
    for peer in candidates[:max_attempts]:
        attempted.append(peer.peer_id)
        try:
            ok = await attempt(peer)
        except Exception:
            ok = False
        if ok:
            emit("failover.ok", {"peer_id": peer.peer_id,
                                  "attempt": len(attempted)})
            return True, peer, attempted
        emit("failover.fail", {"peer_id": peer.peer_id})
    emit("failover.exhausted", {"attempts": len(attempted)})
    return False, None, attempted
