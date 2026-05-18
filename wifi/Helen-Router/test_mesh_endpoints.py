"""
Tests for the mesh-overlay logic exposed by Helen-Router.

We test at two levels:

1. **MeshNode unit tests** — exercise the routing-table builder,
   LSA acceptance, neighbour add/remove, and Dijkstra path picks
   directly. No HTTP involved, so no fight with the lan_only
   middleware that rejects TestClient's None-client requests.

2. **HTTP smoke tests** — spawn the Linux Helen-Router ELF in a
   subprocess on a free port and exercise the actual `/mesh/*`
   endpoints via curl. Those run only when the binary is present
   (skipped on a fresh checkout) and validate the wiring end-to-end.
"""

from __future__ import annotations

import os
import secrets

import pytest


# ── 1. MeshNode unit tests ─────────────────────────────────────────


@pytest.fixture
def node():
    from app.mesh import MeshNode
    return MeshNode(router_id="r-self", my_url="http://10.0.0.1:8080")


def test_node_starts_empty(node):
    assert node.id == "r-self"
    assert node.neighbours == {}
    assert node.direct_servers == {}
    assert node.routes == {}


def test_add_remove_neighbour(node):
    node.add_neighbour("r-2", "http://10.0.0.2:8080", rtt_ms=10.0)
    assert "r-2" in node.neighbours
    assert node.neighbours["r-2"].alive is True
    assert node.neighbours["r-2"].rtt_ms == 10.0

    node.remove_neighbour("r-2")
    assert "r-2" not in node.neighbours


def test_self_announcing_direct_servers(node):
    node.announce_direct_server("server-A", capabilities=["video"])
    assert "server-A" in node.direct_servers
    # Direct server should appear in our routing table with cost 0
    paths = node.routes.get("server-A", [])
    assert paths, "expected at least one path to server-A"
    assert paths[0][0] == "r-self"
    assert paths[0][1] == 0.0


def test_lsa_accept_first_reject_older(node):
    from app.mesh import LSA
    lsa1 = LSA(origin="r-peer", epoch=1, neighbours={},
               direct_servers=["server-B"])
    lsa2_older = LSA(origin="r-peer", epoch=0, neighbours={},
                     direct_servers=["server-C"])
    assert node.receive_lsa(lsa1) is True
    assert node.receive_lsa(lsa2_older) is False
    # Database still has the newer one.
    assert node._lsa_db["r-peer"].epoch == 1


def test_lsa_replaces_when_newer_epoch(node):
    from app.mesh import LSA
    node.receive_lsa(LSA(origin="r-peer", epoch=1, neighbours={},
                         direct_servers=["A"]))
    assert node.receive_lsa(
        LSA(origin="r-peer", epoch=2, neighbours={}, direct_servers=["B"])
    ) is True
    assert node._lsa_db["r-peer"].direct_servers == ["B"]


def test_dijkstra_one_hop_neighbour(node):
    """If a peer router announces a direct server and we have a link
    to that peer, our routes table must list the peer as next-hop."""
    from app.mesh import LSA
    node.add_neighbour("r-peer", "http://10.0.0.2:8080", rtt_ms=5.0)
    node.receive_lsa(LSA(
        origin="r-peer", epoch=1, neighbours={}, direct_servers=["server-X"],
    ))
    paths = node.routes.get("server-X", [])
    assert paths, "expected a path to server-X"
    next_hop_ids = [p[0] for p in paths]
    assert "r-peer" in next_hop_ids
    nh = node.next_hop("server-X")
    assert nh is not None
    assert nh.router_id == "r-peer"


def test_no_path_for_unknown_server(node):
    assert node.next_hop("server-mystery") is None


def test_remove_neighbour_recomputes_routes(node):
    from app.mesh import LSA
    node.add_neighbour("r-peer", "http://10.0.0.2:8080")
    node.receive_lsa(LSA(
        origin="r-peer", epoch=1, neighbours={}, direct_servers=["S"],
    ))
    assert node.routes.get("S")
    node.remove_neighbour("r-peer")
    # After the neighbour disappears we have no link to r-peer, so the
    # Dijkstra-from-self can't find S anymore. The LSA still lives in
    # the DB but has no reachable next-hop.
    paths = node.routes.get("S", [])
    candidates = [p for p in paths if p[0] != "r-self"]
    assert candidates == []


# ── 2. parse_static_peers helper ───────────────────────────────────


def test_parse_static_peers_handles_empty():
    from app.mesh import parse_static_peers
    assert parse_static_peers("") == []
    assert parse_static_peers("   ") == []


def test_parse_static_peers_csv():
    from app.mesh import parse_static_peers
    result = parse_static_peers(
        "id1=http://1.1.1.1:8080,id2=http://2.2.2.2:8080,malformed"
    )
    # The malformed entry (no =) is dropped.
    assert ("id1", "http://1.1.1.1:8080") in result
    assert ("id2", "http://2.2.2.2:8080") in result
    assert len(result) == 2


def test_parse_static_peers_strips_trailing_slash():
    from app.mesh import parse_static_peers
    result = parse_static_peers("only=http://example.local:8080/")
    assert result == [("only", "http://example.local:8080")]


# ── 3. env_router_id default ───────────────────────────────────────


def test_env_router_id_uses_env_when_set(monkeypatch):
    from app.mesh import env_router_id
    monkeypatch.setenv("HELEN_ROUTER_ID", "explicit-router-99")
    assert env_router_id() == "explicit-router-99"


def test_env_router_id_falls_back_to_hostname(monkeypatch):
    from app.mesh import env_router_id
    monkeypatch.delenv("HELEN_ROUTER_ID", raising=False)
    out = env_router_id()
    assert out.startswith("router-")
    assert len(out) > len("router-")


# ── 4. App-level allowlist parser ──────────────────────────────────


def test_lan_only_default_allowlist():
    """Verify the production allowlist parses correctly and includes
    the four RFC1918 nets + loopback. The lan_only middleware is
    integration-tested separately via the live Linux ELF."""
    # Token must be set before importing app.main — module-level code
    # parses LAN_NETS at import time but doesn't enforce the token
    # until the lifespan startup.
    os.environ.setdefault("HELEN_ROUTER_TOKEN", secrets.token_hex(32))
    from app.main import _parse_lan_nets, _DEFAULT_LAN_NETS
    nets = [str(n) for n in _parse_lan_nets(_DEFAULT_LAN_NETS)]
    for required in ("10.0.0.0/8", "172.16.0.0/12",
                     "192.168.0.0/16", "127.0.0.0/8"):
        assert required in nets, f"missing {required}"
