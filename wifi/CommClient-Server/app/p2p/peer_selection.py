"""Peer selection — pick the best K candidates for a workload.

Different selection modes:

  * ``select_for_relay``      — best K relay-capable peers.
  * ``select_for_bridge``     — bridges only, sorted by score.
  * ``select_for_role(role)``  — peers advertising the given role.
  * ``select_random_k``       — uniform random over routable peers.
"""

from __future__ import annotations

import random
from typing import Iterable

from app.p2p.p2p_config import get_config
from app.p2p.p2p_exceptions import PeerSelectionError
from app.p2p.peer_model import Peer, PeerRole
from app.p2p.peer_registry import get_p2p_registry
from app.p2p.peer_scoring import score


def _scored_routable() -> list[tuple[float, Peer]]:
    out = []
    for p in get_p2p_registry().routable():
        s = score(p)
        if s > 0:
            out.append((s, p))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return out


def select_for_relay(k: int | None = None) -> list[Peer]:
    cfg = get_config()
    k = k if k is not None else cfg.selection_top_k
    pairs = _scored_routable()
    relays = [p for s, p in pairs
              if p.role in (PeerRole.RELAY, PeerRole.PROXY,
                            PeerRole.SUPER, PeerRole.BRIDGE)]
    return relays[:k]


def select_for_bridge(k: int | None = None) -> list[Peer]:
    cfg = get_config()
    k = k if k is not None else cfg.selection_top_k
    pairs = _scored_routable()
    return [p for s, p in pairs if p.is_bridge()][:k]


def select_for_role(role: PeerRole, k: int | None = None) -> list[Peer]:
    cfg = get_config()
    k = k if k is not None else cfg.selection_top_k
    pairs = _scored_routable()
    return [p for s, p in pairs if p.role is role][:k]


def select_random_k(k: int = 5) -> list[Peer]:
    routable = get_p2p_registry().routable()
    if not routable:
        return []
    return random.sample(routable, k=min(k, len(routable)))


def select_top_overall(k: int | None = None) -> list[Peer]:
    cfg = get_config()
    k = k if k is not None else cfg.selection_top_k
    return [p for s, p in _scored_routable()[:k]]


def selection_snapshot() -> dict:
    return {
        "top_overall": [p.peer_id for p in select_top_overall(k=10)],
        "relays":      [p.peer_id for p in select_for_relay(k=10)],
        "bridges":     [p.peer_id for p in select_for_bridge(k=10)],
    }
