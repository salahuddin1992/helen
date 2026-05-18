"""Peer discovery — pull from services.peer_registry into p2p registry.

Adapter: bridges the broadcast-payload world (services.peer_registry)
into the behavioural world (p2p.peer_registry). Idempotent — calling
``sync_now()`` repeatedly converges with no side effects.
"""

from __future__ import annotations

import time

from app.p2p.peer_model import Peer, PeerRole
from app.p2p.peer_registry import get_p2p_registry


def _classify_role(record: dict) -> PeerRole:
    """Heuristic mapping from PeerRecord shape → PeerRole."""
    if record.get("bridge"):
        return PeerRole.BRIDGE
    roles = set(record.get("roles") or [])
    if "relay" in roles:
        return PeerRole.RELAY
    if "sfu" in roles:
        return PeerRole.SUPER
    return PeerRole.NORMAL


async def sync_from_services() -> int:
    """Pull every fresh peer from services.peer_registry into the
    p2p layer. Returns the count of newly inserted/updated peers."""
    try:
        from app.services.peer_registry import peer_registry as svc_pr
    except ImportError:
        return 0

    reg = get_p2p_registry()
    n = 0
    try:
        peers = await svc_pr.all_fresh()
    except Exception:
        return 0
    for rec in peers or []:
        try:
            d = rec if isinstance(rec, dict) else rec.__dict__
            host = str(d.get("host") or "")
            if not host:
                continue
            peer = Peer(
                peer_id=str(d.get("server_id") or d.get("peer_id") or ""),
                role=_classify_role(d),
                host=host,
                port=int(d.get("port") or 0),
                cluster_id=str(d.get("cluster_id") or "default"),
                roles=set(d.get("roles") or []),
                bridge_subnets=list(d.get("host_aliases") or []),
                last_seen=float(d.get("last_seen") or time.time()),
                extra={"source": "discovery"},
            )
            if peer.peer_id:
                reg.upsert(peer)
                n += 1
        except Exception:
            continue
    return n


def discovery_snapshot() -> dict:
    return {
        "p2p_registry": get_p2p_registry().snapshot(),
    }
