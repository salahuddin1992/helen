"""Peer-lifecycle state machine.

A peer in the registry moves through:

    DISCOVERED → AUTHENTICATING → ACTIVE ↔ STALE → DEAD

  * DISCOVERED     — first seen, not yet handshaken.
  * AUTHENTICATING — handshake in progress.
  * ACTIVE         — verified, fresh heartbeats.
  * STALE          — no heartbeat for N seconds.
  * DEAD           — failure detector says gone; eligible for eviction.

Listeners on the p2p event bus can react to transitions.
"""

from __future__ import annotations

import threading
from enum import Enum

from app.p2p.peer_events import emit


class PeerState(str, Enum):
    DISCOVERED     = "discovered"
    AUTHENTICATING = "authenticating"
    ACTIVE         = "active"
    STALE          = "stale"
    DEAD           = "dead"


_VALID = {
    PeerState.DISCOVERED:     {PeerState.AUTHENTICATING, PeerState.DEAD},
    PeerState.AUTHENTICATING: {PeerState.ACTIVE, PeerState.DEAD},
    PeerState.ACTIVE:         {PeerState.STALE, PeerState.DEAD},
    PeerState.STALE:          {PeerState.ACTIVE, PeerState.DEAD},
    PeerState.DEAD:           set(),
}


class PeerLifecycle:
    """Per-peer lifecycle tracker. Singleton over peer_id → state."""
    _singleton: "PeerLifecycle | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, PeerState] = {}

    @classmethod
    def instance(cls) -> "PeerLifecycle":
        if cls._singleton is None:
            cls._singleton = PeerLifecycle()
        return cls._singleton

    def state(self, peer_id: str) -> PeerState:
        with self._lock:
            return self._states.get(peer_id, PeerState.DISCOVERED)

    def transition(self, peer_id: str, target: PeerState) -> bool:
        with self._lock:
            current = self._states.get(peer_id, PeerState.DISCOVERED)
            if target == current:
                return True
            if target not in _VALID.get(current, set()):
                return False
            self._states[peer_id] = target
        emit(f"peer.{target.value}", {"peer_id": peer_id})
        return True

    def all_states(self) -> dict[str, str]:
        with self._lock:
            return {k: v.value for k, v in self._states.items()}


def get_lifecycle() -> PeerLifecycle:
    return PeerLifecycle.instance()
