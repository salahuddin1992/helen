"""Per-peer health view — uses path_health + phi_accrual."""

from __future__ import annotations

from app.p2p.peer_model import Peer


def latency_ms(peer: Peer) -> float:
    """EWMA latency for this peer's primary host:port."""
    try:
        from app.services.path_health import get_path_health
        snap = get_path_health().snapshot()
        for entry in snap.get("paths", []):
            if entry.get("key") == f"{peer.host}:{peer.port}":
                return float(entry.get("latency_ms") or 0)
    except Exception:
        pass
    return 0.0


def phi_suspect_level(peer: Peer) -> float:
    try:
        from app.services.phi_accrual import get_phi_registry
        return get_phi_registry().detector_for(peer.peer_id).phi()
    except Exception:
        return 0.0


def is_alive(peer: Peer, threshold: float = 8.0) -> bool:
    return phi_suspect_level(peer) < threshold


def health_snapshot(peer: Peer) -> dict:
    return {
        "peer_id":    peer.peer_id,
        "latency_ms": round(latency_ms(peer), 1),
        "phi":        round(phi_suspect_level(peer), 2),
        "alive":      is_alive(peer),
        "fresh":      peer.is_fresh(),
        "age_sec":    round(peer.freshness_age_sec(), 1),
    }
