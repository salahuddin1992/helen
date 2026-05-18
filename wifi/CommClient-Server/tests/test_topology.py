"""Unit tests for the app.topology package."""

from __future__ import annotations

import time

import pytest

from app.topology.bridge_model import Bridge
from app.topology.link_model import Link, LinkType
from app.topology.node_model import Node, NodeType
from app.topology.subnet_model import (
    Subnet,
    infer_subnet,
    is_loopback,
    is_private,
    same_subnet,
)
from app.topology.topology_graph import TopologyGraph
from app.topology.topology_visualizer import render_ascii, render_mermaid


# ── Subnet helpers ───────────────────────────────────────────────


def test_infer_subnet_basic():
    assert infer_subnet("192.168.1.42") == "192.168.1.0/24"
    assert infer_subnet("10.0.0.1")     == "10.0.0.0/24"
    assert infer_subnet("172.16.7.99")  == "172.16.7.0/24"
    assert infer_subnet("172.16.7.99", default_prefix=16) == "172.16.0.0/16"


def test_infer_subnet_invalid_returns_none():
    assert infer_subnet("not-an-ip") is None
    assert infer_subnet("") is None
    assert infer_subnet("256.0.0.1") is None


def test_same_subnet():
    assert same_subnet("192.168.1.5", "192.168.1.99")
    assert not same_subnet("192.168.1.5", "192.168.2.99")


def test_subnet_membership_via_dataclass():
    s = Subnet(cidr="192.168.1.0/24")
    assert s.contains_ip("192.168.1.55")
    assert not s.contains_ip("10.0.0.1")
    assert not s.contains_ip("not-an-ip")


def test_is_private_loopback():
    assert is_private("192.168.1.1")
    assert not is_private("8.8.8.8")
    assert is_loopback("127.0.0.1")
    assert not is_loopback("192.168.1.1")


# ── Node ────────────────────────────────────────────────────────


def test_node_equality_and_hash_by_id_only():
    a = Node(node_id="X", node_type=NodeType.PEER, host="1.1.1.1")
    b = Node(node_id="X", node_type=NodeType.CLIENT, host="2.2.2.2")
    c = Node(node_id="Y", node_type=NodeType.PEER, host="1.1.1.1")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)


def test_node_serialization_roundtrip():
    n = Node(
        node_id="abc", node_type=NodeType.BRIDGE,
        host="10.0.0.1", port=3000, subnet="10.0.0.0/24",
        roles={"sfu", "relay"},
        capabilities={"cores": 8, "ram_gb": 16.0},
    )
    d = n.to_dict()
    back = Node.from_dict(d)
    assert back.node_id == n.node_id
    assert back.node_type == NodeType.BRIDGE
    assert back.roles == {"sfu", "relay"}
    assert back.capabilities["cores"] == 8


def test_node_freshness():
    n = Node(node_id="t", node_type=NodeType.PEER, host="1", last_seen=time.time())
    assert n.is_fresh(max_age_sec=10)
    n.last_seen = time.time() - 60
    assert not n.is_fresh(max_age_sec=10)


# ── Link ────────────────────────────────────────────────────────


def test_link_key_uniqueness_per_type():
    L1 = Link(src_id="a", dst_id="b", link_type=LinkType.LAN_DIRECT)
    L2 = Link(src_id="a", dst_id="b", link_type=LinkType.BRIDGE)
    L3 = Link(src_id="a", dst_id="b", link_type=LinkType.LAN_DIRECT)
    assert L1.key != L2.key
    assert L1.key == L3.key


def test_link_metrics_record_success_and_failure():
    L = Link(src_id="a", dst_id="b", link_type=LinkType.LAN_DIRECT)
    L.record_success(latency_ms=20)
    assert L.latency_ms == 20
    L.record_success(latency_ms=40)
    assert 20 < L.latency_ms < 40  # EWMA pulled up
    L.record_failure()
    assert L.fail_count == 1
    assert L.packet_loss > 0


# ── Bridge ──────────────────────────────────────────────────────


def test_bridge_promotes_node_type_when_multi_subnet():
    b = Bridge(
        node_id="b1",
        node_type=NodeType.PEER,
        host="10.0.0.1",
        subnets=["192.168.1.0/24", "10.0.0.0/24"],
    )
    assert b.node_type == NodeType.BRIDGE
    assert b.extra.get("bridge") is True


def test_bridge_can_forward_between():
    b = Bridge(
        node_id="b2",
        node_type=NodeType.BRIDGE,
        host="10.0.0.2",
        subnets=["192.168.1.0/24", "10.0.0.0/24"],
    )
    assert b.can_forward_between("192.168.1.0/24", "10.0.0.0/24")
    assert not b.can_forward_between("192.168.1.0/24", "172.16.0.0/24")


# ── Graph ───────────────────────────────────────────────────────


def _build_three_node_graph():
    g = TopologyGraph()
    a = Node(node_id="A", node_type=NodeType.PEER, host="192.168.1.1",
             subnet="192.168.1.0/24")
    b = Node(node_id="B", node_type=NodeType.BRIDGE, host="192.168.1.2",
             subnet="192.168.1.0/24")
    c = Node(node_id="C", node_type=NodeType.PEER, host="10.0.0.1",
             subnet="10.0.0.0/24")
    g.add_node(a); g.add_node(b); g.add_node(c)
    g.add_link(Link(src_id="A", dst_id="B", link_type=LinkType.LAN_DIRECT,
                    latency_ms=2.0))
    g.add_link(Link(src_id="B", dst_id="A", link_type=LinkType.LAN_DIRECT,
                    latency_ms=2.0))
    g.add_link(Link(src_id="B", dst_id="C", link_type=LinkType.BRIDGE,
                    latency_ms=15.0))
    g.add_link(Link(src_id="C", dst_id="B", link_type=LinkType.BRIDGE,
                    latency_ms=15.0))
    return g


def test_graph_neighbors_only_returns_adjacent():
    g = _build_three_node_graph()
    a_neighbors = {n.node_id for n in g.neighbors("A")}
    assert a_neighbors == {"B"}
    b_neighbors = {n.node_id for n in g.neighbors("B")}
    assert b_neighbors == {"A", "C"}


def test_graph_shortest_path_two_hops():
    g = _build_three_node_graph()
    p = g.shortest_path("A", "C")
    assert p == ["A", "B", "C"]


def test_graph_k_shortest_paths_finds_unique_routes():
    g = _build_three_node_graph()
    paths = g.k_shortest_paths("A", "C", k=4)
    assert paths
    assert all(p[0] == "A" and p[-1] == "C" for p in paths)


def test_graph_connected_components_detects_partition():
    g = TopologyGraph()
    g.add_node(Node(node_id="X", node_type=NodeType.PEER, host="1"))
    g.add_node(Node(node_id="Y", node_type=NodeType.PEER, host="2"))
    g.add_node(Node(node_id="Z", node_type=NodeType.PEER, host="3"))
    g.add_link(Link(src_id="X", dst_id="Y", link_type=LinkType.LAN_DIRECT))
    g.add_link(Link(src_id="Y", dst_id="X", link_type=LinkType.LAN_DIRECT))
    comps = g.connected_components()
    assert len(comps) == 2
    assert {len(c) for c in comps} == {2, 1}


def test_graph_stats_shape():
    g = _build_three_node_graph()
    s = g.stats()
    assert s["node_count"] == 3
    assert s["link_count"] == 4
    assert s["components"] == 1
    assert "B" in s["bridges"]


def test_graph_remove_node_drops_links():
    g = _build_three_node_graph()
    g.remove_node("B")
    assert g.node("B") is None
    # All links touching B should be gone.
    for L in g.all_links():
        assert L.src_id != "B"
        assert L.dst_id != "B"


def test_graph_replace_from_dict_restores_state():
    g1 = _build_three_node_graph()
    data = g1.to_dict()
    g2 = TopologyGraph()
    g2.replace_from_dict(data)
    assert g2.stats()["node_count"] == 3
    assert g2.shortest_path("A", "C") == ["A", "B", "C"]


# ── Visualisers ─────────────────────────────────────────────────


def test_render_ascii_contains_headers_and_subnets():
    g = _build_three_node_graph()
    text = render_ascii(g)
    assert "HELEN TOPOLOGY" in text
    assert "192.168.1.0/24" in text
    assert "10.0.0.0/24" in text
    assert "Bridges" in text


def test_render_mermaid_emits_subgraphs_and_edges():
    g = _build_three_node_graph()
    text = render_mermaid(g)
    assert text.startswith("graph LR")
    assert "subgraph subnet_192_168_1_0_24" in text
    assert "subgraph subnet_10_0_0_0_24" in text
    # At least one edge with arrow.
    assert "-->" in text or "==>" in text
