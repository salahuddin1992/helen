"""Peer federation — cross-cluster peer awareness."""

from __future__ import annotations

from app.p2p.peer_identity import my_cluster_id
from app.p2p.peer_model import Peer
from app.p2p.peer_registry import get_p2p_registry


def is_local_cluster(peer: Peer) -> bool:
    return peer.cluster_id == my_cluster_id()


def list_foreign() -> list[Peer]:
    """Peers from a cluster other than ours."""
    me = my_cluster_id()
    return [p for p in get_p2p_registry().all() if p.cluster_id != me]


def list_local() -> list[Peer]:
    return [p for p in get_p2p_registry().all() if is_local_cluster(p)]


def federation_snapshot() -> dict:
    foreign = list_foreign()
    local = list_local()
    by_cluster: dict[str, int] = {}
    for p in foreign:
        by_cluster[p.cluster_id] = by_cluster.get(p.cluster_id, 0) + 1
    return {
        "local_count":   len(local),
        "foreign_count": len(foreign),
        "by_cluster":    by_cluster,
    }
