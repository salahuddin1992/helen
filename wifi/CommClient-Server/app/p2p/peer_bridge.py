"""Peer bridge — discovery + selection of cross-subnet bridges."""

from __future__ import annotations

from app.p2p.peer_model import Peer
from app.p2p.peer_registry import get_p2p_registry
from app.p2p.peer_scoring import score


def list_bridges() -> list[Peer]:
    return get_p2p_registry().bridges()


def best_bridge_for_subnet(target_subnet: str) -> Peer | None:
    candidates = [p for p in list_bridges()
                  if target_subnet in (p.bridge_subnets or [])]
    if not candidates:
        candidates = list_bridges()
    if not candidates:
        return None
    candidates.sort(key=score, reverse=True)
    return candidates[0]


def bridge_snapshot() -> dict:
    return {
        "count":  len(list_bridges()),
        "ids":    [b.peer_id for b in list_bridges()],
        "subnets": sorted({s for b in list_bridges()
                           for s in (b.bridge_subnets or [])}),
    }
