"""Tests for app.p2p.peer_scoring."""

from __future__ import annotations

from app.p2p.peer_model import Peer, PeerRole
from app.p2p.peer_scoring import explain, score


def _peer(role: PeerRole = PeerRole.NORMAL,
          host: str = "10.0.0.1",
          quarantined: bool = False) -> Peer:
    return Peer(
        peer_id=f"score-{role.value}",
        role=PeerRole.QUARANTINED if quarantined else role,
        host=host, port=3000,
        capabilities={"cpu_cores": 4, "ram_gb": 8.0, "nic_gbps": 1.0},
    )


def test_score_quarantined_is_zero():
    p = _peer(quarantined=True)
    assert score(p) == 0.0


def test_score_normal_peer_above_zero():
    p = _peer(role=PeerRole.NORMAL)
    s = score(p)
    assert 0 < s <= 2


def test_super_peer_outranks_normal():
    sup = _peer(role=PeerRole.SUPER)
    nor = _peer(role=PeerRole.NORMAL)
    assert score(sup) > score(nor)


def test_explain_returns_breakdown():
    p = _peer(role=PeerRole.RELAY)
    e = explain(p)
    assert "score" in e
    if e.get("rejected") is None:
        assert {"trust", "health", "phi", "cap", "fresh", "bridge"}.issubset(e.keys())


def test_unroutable_peer_is_zero():
    p = _peer(host="")  # no host
    assert score(p) == 0.0
