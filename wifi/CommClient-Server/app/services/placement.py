"""
Placement scorer — picks the strongest+healthiest node for a new room.

Policy (per the user's directive):
  "إذا كان هناك أكثر من حاسوب أو سيرفر متصل، فالأفضلية تذهب للأقوى والأفضل"
  → strength weight dominates when headroom is comparable;
    headroom still acts as a brake on already-hot nodes.

Entry point: place(room_request) → PlacementResult
Callers (channel creation, call start) consult this before committing
state so admission and node selection are a single atomic decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from app.services.node_registry import (
    Node,
    compute_strength,
    compute_headroom,
    get_registry,
)

logger = structlog.get_logger(__name__)


@dataclass
class RoomRequest:
    kind:             str             # chat | audio | video | broadcast | file
    participants_est: int = 2
    priority:         str = "normal"   # normal | high | critical
    creator_node_id:  Optional[str] = None


@dataclass
class PlacementResult:
    assigned:       bool
    node_id:        Optional[str]
    node_host:      Optional[str]
    reason:         str
    score:          float
    alternatives:   list[dict]
    refused_reason: Optional[str] = None


# Per-kind cost estimates (used for admission feasibility; fine-grained
# resource reservation happens once the SFU/Relay actually provision).
COST_BY_KIND = {
    "chat":      {"cpu_pct": 0.1, "nic_mbps": 0.05, "needs_sfu": False, "needs_file": False},
    "audio":     {"cpu_pct": 3.0, "nic_mbps": 0.1,  "needs_sfu": False, "needs_file": False},
    "video":     {"cpu_pct": 18.0,"nic_mbps": 4.0,  "needs_sfu": True,  "needs_file": False},
    "broadcast": {"cpu_pct": 25.0,"nic_mbps": 8.0,  "needs_sfu": True,  "needs_file": False},
    "file":      {"cpu_pct": 2.0, "nic_mbps": 20.0, "needs_sfu": False, "needs_file": True},
}


def _node_can_host(n: Node, req: RoomRequest) -> tuple[bool, str]:
    if n.is_dead():
        return False, "node_dead"
    if n.load.phase == "frozen":
        return False, "node_frozen"
    if n.load.phase == "emergency" and req.kind in ("video", "broadcast"):
        return False, "emergency_media_blocked"
    cost = COST_BY_KIND.get(req.kind, COST_BY_KIND["chat"])
    if cost["needs_sfu"] and not n.roles.sfu:
        return False, "no_sfu_role"
    if cost["needs_file"] and not n.roles.file_transfer:
        return False, "no_file_role"
    # Per-participant cost scales inversely with core count — a 32-core box
    # handles codec work roughly 8× faster than a 4-core box, so the same
    # video call adds a fraction of the %cpu load.
    cpu_per_peer = cost["cpu_pct"] * (4.0 / max(1, n.capability.cpu_cores))
    projected_cpu = n.load.cpu_pct + cpu_per_peer * req.participants_est
    if projected_cpu > 95:
        return False, "would_saturate_cpu"
    # NIC check — node cap vs projected traffic fan-out.
    cap_mbps = n.capability.nic_gbps * 1000
    projected_nic = n.load.nic_tx_mbps + cost["nic_mbps"] * req.participants_est
    if projected_nic > cap_mbps * 0.9:
        return False, "would_saturate_nic"
    return True, "ok"


def _score(n: Node, req: RoomRequest) -> float:
    strength = compute_strength(n.capability)
    headroom = compute_headroom(n.load)
    # Affinity: same node as the creator's socket gets a boost (reduces
    # cross-node hops for the control plane).
    affinity = 1.15 if (req.creator_node_id and
                        n.node_id == req.creator_node_id) else 1.0
    # Priority bonus: critical rooms prefer the strongest node even more
    # (critical_weight = 1.3 pushes strength over headroom).
    priority = {"normal": 1.0, "high": 1.1, "critical": 1.3}.get(
        req.priority, 1.0)
    return round(strength * headroom * affinity * priority, 3)


def place(req: RoomRequest) -> PlacementResult:
    reg = get_registry()
    # Keep self-load fresh before scoring.
    reg.refresh_self_load()
    nodes = reg.nodes(include_dead=False)
    if not nodes:
        return PlacementResult(
            assigned=False, node_id=None, node_host=None,
            reason="no_nodes_registered", score=0.0, alternatives=[],
            refused_reason="empty_pool",
        )
    scored: list[tuple[Node, float, str]] = []
    for n in nodes:
        ok, reason = _node_can_host(n, req)
        if not ok:
            scored.append((n, 0.0, reason))
            continue
        scored.append((n, _score(n, req), "ok"))
    scored.sort(key=lambda t: t[1], reverse=True)
    best = scored[0]
    if best[1] <= 0:
        # Offer a degraded fallback reason.
        refused = best[2] if best[1] == 0 else "no_capable_node"
        return PlacementResult(
            assigned=False, node_id=None, node_host=None,
            reason="refused", score=0.0,
            alternatives=[{"node_id": n.node_id, "score": s, "reason": r}
                          for n, s, r in scored[:5]],
            refused_reason=refused,
        )
    alternatives = [
        {"node_id": n.node_id, "host": n.host, "score": s, "reason": r}
        for n, s, r in scored[:5]
    ]
    logger.info("placement_decided",
                room_kind=req.kind, participants=req.participants_est,
                chosen_node=best[0].node_id, score=best[1])
    return PlacementResult(
        assigned=True,
        node_id=best[0].node_id,
        node_host=best[0].host,
        reason="best_score",
        score=best[1],
        alternatives=alternatives,
    )


def preview_candidates(req: RoomRequest) -> list[dict]:
    """Same scoring but returns the full table for UI display; never
    mutates state. Used by /api/admin/placement/preview.
    """
    reg = get_registry()
    reg.refresh_self_load()
    nodes = reg.nodes(include_dead=True)
    out = []
    for n in nodes:
        ok, reason = _node_can_host(n, req)
        score = _score(n, req) if ok else 0.0
        out.append({
            "node_id":   n.node_id,
            "host":      n.host,
            "self_node": n.self_node,
            "strength":  compute_strength(n.capability),
            "headroom":  compute_headroom(n.load),
            "score":     score,
            "ok":        ok,
            "reason":    reason,
            "phase":     n.load.phase,
            "cpu_pct":   n.load.cpu_pct,
            "rss_pct":   n.load.rss_pct,
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out
