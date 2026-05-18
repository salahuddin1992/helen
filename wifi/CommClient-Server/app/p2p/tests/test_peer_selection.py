"""Tests for app.p2p.peer_selection."""

from __future__ import annotations

from app.p2p.peer_model import Peer, PeerRole
from app.p2p.peer_registry import P2PPeerRegistry, get_p2p_registry
from app.p2p.peer_selection import (
    select_for_bridge, select_for_relay, select_for_role,
    select_random_k, select_top_overall, selection_snapshot,
)


def _seed_some_peers():
    reg = get_p2p_registry()
    # Seed a few peers with different roles.
    reg.upsert(Peer(peer_id="sel-relay-A", role=PeerRole.RELAY,
                    host="10.0.0.1", port=3000))
    reg.upsert(Peer(peer_id="sel-bridge-B", role=PeerRole.BRIDGE,
                    host="10.0.0.2", port=3000,
                    bridge_subnets=["10.0.0.0/24", "192.168.1.0/24"]))
    reg.upsert(Peer(peer_id="sel-normal-C", role=PeerRole.NORMAL,
                    host="10.0.0.3", port=3000))


def test_select_for_relay_returns_list():
    _seed_some_peers()
    out = select_for_relay(k=5)
    assert isinstance(out, list)


def test_select_for_bridge_filters_to_bridges():
    _seed_some_peers()
    bridges = select_for_bridge(k=5)
    for p in bridges:
        assert p.is_bridge()


def test_select_for_role_filters_correctly():
    _seed_some_peers()
    relays = select_for_role(PeerRole.RELAY, k=5)
    for p in relays:
        assert p.role is PeerRole.RELAY


def test_select_random_k_capped():
    _seed_some_peers()
    out = select_random_k(k=2)
    assert len(out) <= 2


def test_select_top_overall_returns_capped_list():
    _seed_some_peers()
    out = select_top_overall(k=3)
    assert len(out) <= 3


def test_snapshot_keys():
    _seed_some_peers()
    s = selection_snapshot()
    assert {"top_overall", "relays", "bridges"}.issubset(s.keys())
