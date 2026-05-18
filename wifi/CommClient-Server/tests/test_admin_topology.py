"""
Tests for the admin Network Topology Visualizer router + services.

The router is mounted standalone on a fresh FastAPI app so we exercise:
    * route wiring + prefixes,
    * RBAC (``require_role("admin")``) — wrong role → 403, missing → 401/403,
    * graceful degradation when federation / overlay / p2p are missing,
    * job-runner contract (status, result, audit hooks).

Authentication uses ``app.core.security.create_access_token(..., role="admin")``
so the real JWT verification dependency is exercised.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


def _make_app() -> FastAPI:
    from app.api.routes.admin_topology import router as topology_router
    app = FastAPI()
    app.include_router(topology_router)
    return app


def _admin_headers() -> dict[str, str]:
    from app.core.security import create_access_token
    tok = create_access_token("admin-test-uid", role="admin")
    return {"Authorization": f"Bearer {tok}"}


def _user_headers() -> dict[str, str]:
    from app.core.security import create_access_token
    tok = create_access_token("user-test-uid", role="user")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def client():
    app = _make_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def headers():
    return _admin_headers()


# ─────────────────────────────────────────────────────────────
# /topology/graph
# ─────────────────────────────────────────────────────────────


def test_graph_returns_nodes_and_edges(client, headers):
    r = client.get("/api/admin/topology/graph", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "nodes" in data and isinstance(data["nodes"], list)
    assert "edges" in data and isinstance(data["edges"], list)
    assert "flags" in data and isinstance(data["flags"], dict)
    # The self-server node should always be present.
    assert data["node_count"] >= 1
    server_nodes = [n for n in data["nodes"] if n["type"] == "server"]
    assert len(server_nodes) >= 1


def test_graph_requires_auth(client):
    r = client.get("/api/admin/topology/graph")
    assert r.status_code in (401, 403)


def test_graph_rejects_non_admin(client):
    r = client.get("/api/admin/topology/graph", headers=_user_headers())
    assert r.status_code == 403


def test_graph_force_refresh_query_param(client, headers):
    r = client.get(
        "/api/admin/topology/graph",
        params={"refresh": "true"},
        headers=headers,
    )
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────
# /topology/nodes — filtering
# ─────────────────────────────────────────────────────────────


def test_nodes_endpoint_basic(client, headers):
    r = client.get("/api/admin/topology/nodes", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "count" in data and "flags" in data
    assert data["count"] == len(data["nodes"])


def test_nodes_filter_by_type(client, headers):
    r = client.get(
        "/api/admin/topology/nodes",
        params={"type": "server"},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    for n in data["nodes"]:
        assert n["type"] == "server"


def test_nodes_filter_by_status(client, headers):
    r = client.get(
        "/api/admin/topology/nodes",
        params={"status": "up"},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    for n in data["nodes"]:
        assert n["status"] == "up"


def test_nodes_search_substring(client, headers):
    r = client.get(
        "/api/admin/topology/nodes",
        params={"search": "server"},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    for n in data["nodes"]:
        text = (n["id"] + n["hostname"] + n["ip"]).lower()
        assert "server" in text


# ─────────────────────────────────────────────────────────────
# /topology/links — filtering
# ─────────────────────────────────────────────────────────────


def test_links_endpoint_basic(client, headers):
    r = client.get("/api/admin/topology/links", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "edges" in data and "count" in data
    assert data["count"] == len(data["edges"])


def test_links_filter_by_transport(client, headers):
    r = client.get(
        "/api/admin/topology/links",
        params={"transport": "cluster"},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    for e in data["edges"]:
        assert e["transport"] == "cluster"


def test_links_latency_range(client, headers):
    r = client.get(
        "/api/admin/topology/links",
        params={"minLatency": 0, "maxLatency": 10000},
        headers=headers,
    )
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────
# /topology/path
# ─────────────────────────────────────────────────────────────


def test_path_endpoint_self_loop(client, headers):
    # First grab a known node id.
    g = client.get("/api/admin/topology/graph", headers=headers).json()
    if not g["nodes"]:
        pytest.skip("empty graph — environment-specific")
    nid = g["nodes"][0]["id"]
    r = client.get(
        "/api/admin/topology/path",
        params={"src": nid, "dst": nid},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    assert data["hop_count"] == 0


def test_path_unknown_endpoint_returns_not_found_flag(client, headers):
    r = client.get(
        "/api/admin/topology/path",
        params={"src": "does-not-exist", "dst": "also-missing"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_path_invalid_weight_returns_400(client, headers):
    g = client.get("/api/admin/topology/graph", headers=headers).json()
    if not g["nodes"]:
        pytest.skip("empty graph")
    nid = g["nodes"][0]["id"]
    r = client.get(
        "/api/admin/topology/path",
        params={"src": nid, "dst": nid, "weight": "BOGUS"},
        headers=headers,
    )
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────
# /topology/action + /topology/jobs/{id}
# ─────────────────────────────────────────────────────────────


def test_action_unknown_returns_400(client, headers):
    r = client.post(
        "/api/admin/topology/action",
        json={"node_id": "server:test", "action": "nuke"},
        headers=headers,
    )
    assert r.status_code == 400


def test_action_ping_returns_job_id(client, headers):
    r = client.post(
        "/api/admin/topology/action",
        json={
            "node_id": "server:127.0.0.1",
            "action":  "ping",
            "params":  {"host": "127.0.0.1", "timeout": 5},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "job_id" in data
    assert data["action"] == "ping"
    assert data["node_id"] == "server:127.0.0.1"
    assert data["status"] in (
        "pending", "running", "success", "failed", "timed_out",
    )


def test_action_job_lookup(client, headers):
    # Trigger then poll.
    r = client.post(
        "/api/admin/topology/action",
        json={
            "node_id": "server:127.0.0.1",
            "action":  "ping",
            "params":  {"host": "127.0.0.1", "timeout": 5},
        },
        headers=headers,
    )
    assert r.status_code == 200
    jid = r.json()["job_id"]

    # The job should be queryable immediately.
    r2 = client.get(f"/api/admin/topology/jobs/{jid}", headers=headers)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["job_id"] == jid
    assert body["action"] == "ping"


def test_action_job_404_for_unknown(client, headers):
    r = client.get(
        "/api/admin/topology/jobs/nonexistent-jobid",
        headers=headers,
    )
    assert r.status_code == 404


def test_action_requires_admin(client):
    r = client.post(
        "/api/admin/topology/action",
        json={"node_id": "x", "action": "ping"},
        headers=_user_headers(),
    )
    assert r.status_code == 403


# ─────────────────────────────────────────────────────────────
# Sub-service proxies — graceful degradation
# ─────────────────────────────────────────────────────────────


def test_federation_peers_endpoint(client, headers):
    r = client.get("/api/admin/federation/peers", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "peers" in data and "count" in data
    assert isinstance(data["peers"], list)
    # Flag must be present whether enabled or disabled.
    assert "federation_disabled" in data


def test_overlay_sessions_endpoint(client, headers):
    r = client.get("/api/admin/overlay/sessions", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "count" in data and "sessions" in data
    assert "overlay_disabled" in data


def test_p2p_dht_snapshot_endpoint(client, headers):
    r = client.get("/api/admin/p2p/dht/snapshot", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "dht_disabled" in data


# ─────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────


def test_ws_topology_requires_token(client):
    # No token → close with 4401.
    with pytest.raises(Exception):
        with client.websocket_connect("/api/admin/ws/topology") as ws:
            ws.receive_text()


def test_ws_topology_admin_receives_initial_graph(client):
    from app.core.security import create_access_token
    tok = create_access_token("admin-ws-uid", role="admin")
    with client.websocket_connect(
        f"/api/admin/ws/topology?token={tok}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "graph.full"
        assert "graph" in msg
        assert "nodes" in msg["graph"]
        assert "edges" in msg["graph"]


def test_ws_topology_rejects_non_admin(client):
    from app.core.security import create_access_token
    tok = create_access_token("user-ws-uid", role="user")
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/api/admin/ws/topology?token={tok}"
        ) as ws:
            ws.receive_text()


def test_ws_topology_ping_pong(client):
    from app.core.security import create_access_token
    tok = create_access_token("admin-ws-uid", role="admin")
    with client.websocket_connect(
        f"/api/admin/ws/topology?token={tok}"
    ) as ws:
        # Initial graph.full.
        _ = ws.receive_json()
        ws.send_json({"type": "ping"})
        # The next frame should be pong (or graph.full from refresh — both ok)
        frame = ws.receive_json()
        assert frame["type"] in ("pong", "graph.full", "heartbeat")


# ─────────────────────────────────────────────────────────────
# Service layer unit tests (no HTTP)
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregator_builds_graph_with_self_node():
    from app.services.topology.aggregator import TopologyAggregator
    agg = TopologyAggregator(cache_ttl=0.1)
    graph = await agg.build_graph(force_refresh=True)
    assert any(n.type == "server" for n in graph.nodes)
    assert graph.build_time_ms >= 0
    assert isinstance(graph.flags, dict)


@pytest.mark.asyncio
async def test_aggregator_cache_returns_same_object():
    from app.services.topology.aggregator import TopologyAggregator
    agg = TopologyAggregator(cache_ttl=60.0)
    g1 = await agg.build_graph(force_refresh=True)
    g2 = await agg.build_graph()
    assert g1 is g2


@pytest.mark.asyncio
async def test_pathfinder_self_loop():
    from app.services.topology.aggregator import (
        TopologyAggregator, TopologyNode, LAYER_PHYSICAL,
    )
    from app.services.topology.pathfinder import Pathfinder
    g = await TopologyAggregator(cache_ttl=0.1).build_graph(force_refresh=True)
    if not g.nodes:
        pytest.skip("empty graph")
    nid = g.nodes[0].id
    res = Pathfinder.find_path(g, nid, nid)
    assert res.found
    assert res.hop_count == 0


@pytest.mark.asyncio
async def test_pathfinder_synthetic_two_node_graph():
    from app.services.topology.aggregator import (
        TopologyGraph, TopologyNode, TopologyLink,
        LAYER_PHYSICAL, NODE_TYPE_SERVER, NODE_TYPE_ROUTER,
    )
    from app.services.topology.pathfinder import Pathfinder

    g = TopologyGraph()
    g.nodes.append(TopologyNode(id="A", type=NODE_TYPE_SERVER,
                                layer=LAYER_PHYSICAL))
    g.nodes.append(TopologyNode(id="B", type=NODE_TYPE_ROUTER,
                                layer=LAYER_PHYSICAL))
    g.edges.append(TopologyLink(
        src="A", dst="B", transport="cluster",
        layer=LAYER_PHYSICAL, rtt_ms=12.5,
    ))
    res = Pathfinder.find_path(g, "A", "B", weight="rtt")
    assert res.found
    assert res.hop_count == 1
    assert res.hops[0].rtt_ms == 12.5
    assert res.total_rtt_ms == pytest.approx(12.5, rel=1e-3)


@pytest.mark.asyncio
async def test_actions_run_job_unknown_action_raises():
    from app.services.topology.actions import TopologyActions
    a = TopologyActions()
    with pytest.raises(ValueError):
        await a.run_job("server:x", "nuke")


@pytest.mark.asyncio
async def test_actions_run_job_ping_completes():
    from app.services.topology.actions import (
        TopologyActions, JOB_STATUS_SUCCESS, JOB_STATUS_FAILED,
        JOB_STATUS_TIMED_OUT,
    )
    a = TopologyActions()
    job = await a.run_job(
        node_id="server:127.0.0.1",
        action="ping",
        params={"host": "127.0.0.1", "timeout": 8},
        user_id="test-admin",
    )
    # Wait for completion (poll up to 10 s).
    for _ in range(40):
        if a.get(job.job_id) and a.get(job.job_id).finished:
            break
        await asyncio.sleep(0.25)
    final = a.get(job.job_id)
    assert final is not None
    assert final.status in (
        JOB_STATUS_SUCCESS, JOB_STATUS_FAILED, JOB_STATUS_TIMED_OUT,
    )
