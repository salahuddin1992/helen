"""Peer scoring — weighted composite of trust + health + capability.

Score in [0, 1+]. Used by peer_selection to rank candidates. Returns
0 for quarantined peers, peers with phi above the ceiling, or peers
with trust below the floor.
"""

from __future__ import annotations

from app.p2p.p2p_config import get_config
from app.p2p.peer_model import Peer


# Internal weights — sum normalised at score time.
_W_TRUST    = 0.30
_W_HEALTH   = 0.25
_W_CAP      = 0.20
_W_FRESH    = 0.10
_W_BRIDGE   = 0.10
_W_ROLE     = 0.05


def _trust_for(peer: Peer) -> float:
    try:
        from app.services.trust_score import get_trust_db
        return float(get_trust_db().get_score(peer.peer_id))
    except Exception:
        return 0.5


def _health_for(peer: Peer) -> float:
    try:
        from app.services.path_health import get_path_health
        return min(2.0, get_path_health().latency_score(peer.host, peer.port)) / 2.0
    except Exception:
        return 0.5


def _phi_for(peer: Peer) -> float:
    try:
        from app.services.phi_accrual import get_phi_registry
        return get_phi_registry().detector_for(peer.peer_id).phi()
    except Exception:
        return 0.0


def _capability_score(peer: Peer) -> float:
    cores = float(peer.capabilities.get("cpu_cores") or 1)
    nic   = float(peer.capabilities.get("nic_gbps") or 0.1)
    raw   = cores * nic
    return min(1.0, raw / (raw + 14.0))


def _freshness_score(peer: Peer) -> float:
    age = peer.freshness_age_sec()
    if age < 30:
        return 1.0
    if age < 120:
        return 0.7
    if age < 600:
        return 0.4
    return 0.1


def _role_bonus(peer: Peer) -> float:
    """Roles a Helen-Server is most useful in for cross-peer routing."""
    return {
        "super":         1.0,
        "bridge":        1.0,
        "relay":         0.85,
        "proxy":         0.85,
        "federation":    0.75,
        "dht":           0.65,
        "gateway":       0.65,
        "normal":        0.5,
        "quarantined":   0.0,
    }.get(peer.role.value, 0.5)


def score(peer: Peer) -> float:
    """Composite score; 0 means rejected."""
    cfg = get_config()
    if peer.is_quarantined() or not peer.is_routable():
        return 0.0
    trust = _trust_for(peer)
    if trust < cfg.selection_min_trust:
        return 0.0
    phi = _phi_for(peer)
    if phi >= cfg.selection_max_phi:
        return 0.0
    health = _health_for(peer)
    cap    = _capability_score(peer)
    fresh  = _freshness_score(peer)
    bridge = 1.0 if peer.is_bridge() else 0.5
    role   = _role_bonus(peer)
    raw = (
        _W_TRUST  * trust  +
        _W_HEALTH * health +
        _W_CAP    * cap    +
        _W_FRESH  * fresh  +
        _W_BRIDGE * bridge +
        _W_ROLE   * role
    )
    peer.score = round(raw, 6)
    return peer.score


def explain(peer: Peer) -> dict:
    """Return a per-factor breakdown for diagnostics. Pure — does NOT
    mutate ``peer.score``; safe to call from admin endpoints without
    side effects on routing decisions."""
    cfg = get_config()
    if peer.is_quarantined():
        return {"score": 0.0, "rejected": "quarantined"}
    if not peer.is_routable():
        return {"score": 0.0, "rejected": "not_routable"}
    trust = _trust_for(peer)
    phi = _phi_for(peer)
    breakdown = {
        "trust":    round(trust, 3),
        "health":   round(_health_for(peer), 3),
        "phi":      round(phi, 2),
        "cap":      round(_capability_score(peer), 3),
        "fresh":    round(_freshness_score(peer), 3),
        "bridge":   1.0 if peer.is_bridge() else 0.5,
        "role":     round(_role_bonus(peer), 3),
    }
    if trust < cfg.selection_min_trust:
        return {"score": 0.0, "rejected": f"trust_below_floor:{trust:.3f}",
                **breakdown}
    if phi >= cfg.selection_max_phi:
        return {"score": 0.0, "rejected": f"phi_above_ceiling:{phi:.2f}",
                **breakdown}
    raw = (
        _W_TRUST  * trust                    +
        _W_HEALTH * breakdown["health"]      +
        _W_CAP    * breakdown["cap"]         +
        _W_FRESH  * breakdown["fresh"]       +
        _W_BRIDGE * breakdown["bridge"]      +
        _W_ROLE   * breakdown["role"]
    )
    return {"score": round(raw, 6), **breakdown}
