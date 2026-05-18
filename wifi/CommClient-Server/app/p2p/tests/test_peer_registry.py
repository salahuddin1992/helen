"""Tests for app.p2p.peer_registry."""

from __future__ import annotations

import pytest

from app.p2p.p2p_exceptions import PeerNotFoundError
from app.p2p.peer_model import Peer, PeerRole
from app.p2p.peer_registry import P2PPeerRegistry, get_p2p_registry


def _peer(pid: str, role: PeerRole = PeerRole.NORMAL,
          host: str = "1.2.3.4") -> Peer:
    return Peer(peer_id=pid, role=role, host=host, port=3000)


def test_singleton_identity():
    assert get_p2p_registry() is P2PPeerRegistry.instance()


def test_upsert_and_get():
    reg = P2PPeerRegistry()
    reg.upsert(_peer("a"))
    assert reg.get("a") is not None
    assert reg.get("missing") is None


def test_require_raises_for_missing():
    reg = P2PPeerRegistry()
    with pytest.raises(PeerNotFoundError):
        reg.require("missing-xx")


def test_upsert_merges_existing():
    reg = P2PPeerRegistry()
    p1 = _peer("merge", host="1.1.1.1")
    p1.roles = {"signaling"}
    reg.upsert(p1)
    p2 = _peer("merge", host="1.1.1.1")
    p2.roles = {"sfu"}
    reg.upsert(p2)
    got = reg.require("merge")
    assert got.roles == {"signaling", "sfu"}


def test_filter_by_role():
    reg = P2PPeerRegistry()
    reg.upsert(_peer("normal", PeerRole.NORMAL))
    reg.upsert(_peer("relay",  PeerRole.RELAY))
    reg.upsert(_peer("bridge", PeerRole.BRIDGE))
    assert {p.peer_id for p in reg.by_role(PeerRole.RELAY)} == {"relay"}
    assert {p.peer_id for p in reg.bridges()} == {"bridge"}


def test_remove_returns_bool():
    reg = P2PPeerRegistry()
    reg.upsert(_peer("rm"))
    assert reg.remove("rm") is True
    assert reg.remove("rm") is False


def test_snapshot_shape():
    reg = P2PPeerRegistry()
    reg.upsert(_peer("snap-a"))
    s = reg.snapshot()
    expected = {"count", "count_by_role", "fresh", "bridges", "quarantined"}
    assert expected.issubset(s.keys())
