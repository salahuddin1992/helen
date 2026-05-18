"""
Cluster-aware load balancer — weight proxies by capacity and live load.

When the relay chain falls back to multi-hop, ``cluster_mesh`` orders
proxy candidates by latency score (path_health). That's correct for
*reachability* but blind to *load*: it'd happily route 100 concurrent
calls through the fastest proxy while four slightly slower bridges
sit idle.

This module produces a **weighted ordering** that combines:

  * latency_score   — from path_health (0 = failed, ~2 = fast & proven)
  * trust_score     — from trust_score DB    (0..1, default 0.5)
  * headroom        — derived from NodeLoad   (0..1, 0 = saturated)
  * capacity        — node capability factor  (cores, NIC)
  * is_bridge       — multiplier for bridge nodes (cross-subnet bonus)

The combined ``proxy_weight`` feeds into the relay chain so a 14-core
idle bridge wins over a 4-core fully-loaded node even if their
latencies are equal.

The function is pure — given the same inputs it returns the same
ordering, so callers can cache for short windows if they like. We
intentionally avoid randomization here; for spreading traffic across
similar-weighted proxies, the caller draws from the top-K weighted
slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.core.logging import get_logger

logger = get_logger(__name__)


# Weights — should sum to ~1.0 but not strictly required.
W_LATENCY  = 0.30
W_TRUST    = 0.20
W_HEADROOM = 0.25
W_CAPACITY = 0.15
W_BRIDGE   = 0.10  # multiplicative bonus, applied at the end


@dataclass
class ScoredProxy:
    node: object  # Node from node_registry
    weight: float
    breakdown: dict


def _normalize_capacity(node) -> float:
    """Map (cores × nic_gbps) into 0..1 with 14-core / 1-Gbps box ≈ 0.5."""
    try:
        cap = node.capability
        cores = max(1, int(getattr(cap, "cpu_cores", 1)))
        nic = max(0.1, float(getattr(cap, "nic_gbps", 1.0)))
        raw = cores * nic
        # 14×1 = 14 → 0.5; 14×10 = 140 → ~0.93
        return min(1.0, raw / (raw + 14.0))
    except Exception:
        return 0.5


def _node_headroom(node) -> float:
    """Re-use node_registry.compute_headroom if available; fall back
    to 0.5 for unknown loads."""
    try:
        from app.services.node_registry import compute_headroom
        return compute_headroom(node.load)
    except Exception:
        return 0.5


def _is_bridge(node) -> bool:
    try:
        return bool((node.extra or {}).get("bridge", False))
    except Exception:
        return False


def score_proxy(node) -> ScoredProxy:
    """Compute weighted score for a single proxy candidate."""
    try:
        from app.services.path_health import get_path_health
        from app.services.trust_score import get_trust_db
    except ImportError:
        return ScoredProxy(node=node, weight=0.5, breakdown={})

    health = get_path_health()
    trust  = get_trust_db()

    latency = health.latency_score(node.host, node.port)  # 0..2
    trust_s = trust.get_score(node.node_id)                # 0..1
    head    = _node_headroom(node)                         # 0..1
    cap     = _normalize_capacity(node)                    # 0..1
    bridge  = _is_bridge(node)

    base = (
        W_LATENCY  * (latency / 2.0) +     # rescale to 0..1
        W_TRUST    * trust_s +
        W_HEADROOM * head +
        W_CAPACITY * cap
    )
    # Bridges get a +10% nudge because cross-subnet reach is the
    # main reason we have multi-hop relay in the first place.
    weight = base * (1.0 + W_BRIDGE) if bridge else base

    return ScoredProxy(
        node=node,
        weight=round(weight, 5),
        breakdown={
            "latency": round(latency, 3),
            "trust":   round(trust_s, 3),
            "head":    round(head, 3),
            "cap":     round(cap, 3),
            "bridge":  bridge,
            "base":    round(base, 5),
        },
    )


def rank_proxies(nodes: Iterable, top_k: int = 8) -> list[ScoredProxy]:
    """Return the top-K proxies sorted by weight, best first.

    Failed paths (latency_score == 0) are dropped — load balancing
    only happens between healthy candidates.
    """
    scored = [score_proxy(n) for n in nodes]
    alive = [s for s in scored if s.breakdown.get("latency", 0) > 0]
    alive.sort(key=lambda s: s.weight, reverse=True)
    return alive[:top_k]


def snapshot(nodes: Iterable) -> dict:
    """Diagnostic dump for the admin endpoint."""
    ranked = rank_proxies(nodes, top_k=50)
    return {
        "weights": {
            "latency":  W_LATENCY,
            "trust":    W_TRUST,
            "headroom": W_HEADROOM,
            "capacity": W_CAPACITY,
            "bridge_bonus": W_BRIDGE,
        },
        "candidates": [
            {
                "node_id":   s.node.node_id,
                "host":      s.node.host,
                "port":      s.node.port,
                "weight":    s.weight,
                "breakdown": s.breakdown,
            }
            for s in ranked
        ],
    }
