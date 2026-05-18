"""
Tests for the Federation Health-Map admin router (``/api/admin/federation``).

Coverage (auth, peers, sync, metrics, shaper, certs, quarantine,
roles, audit, diagnose, topology, replication, policies, quorum, WS).

We piggyback on the project's standard async test fixtures:
* ``db_session``        — fresh in-memory async DB per test
* ``client``            — httpx AsyncClient bound to a FastAPI app
* ``create_access_token`` — issue admin/user JWTs

All federation services consume the global ``async_session_factory``;
we point that at the test DB's factory for the duration of the test.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.main import create_app
from app.core.deps import get_db


# ─────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────


def _admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token('admin-uid', role='admin')}"
    }


def _user_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token('user-uid', role='user')}"
    }


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
async def fed_client(db_session, monkeypatch):
    """``AsyncClient`` wired to a fresh app that shares ``db_session``'s
    engine across all federation_v2 services + reset all service
    singletons so tests don't leak state.
    """
    # Build a factory that yields short-lived sessions on the shared engine.
    engine = db_session.bind
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Patch the global ``async_session_factory`` in every federation
    # service module that imported it.
    import app.db.session as _session
    for mod_path in (
        "app.db.session",
        "app.services.federation_v2.peer_manager",
        "app.services.federation_v2.replication_monitor",
        "app.services.federation_v2.shaper",
        "app.services.federation_v2.cert_manager",
        "app.services.federation_v2.quorum",
        "app.services.federation_v2.policy_engine",
        "app.services.federation_v2.diagnostics",
    ):
        try:
            mod = __import__(mod_path, fromlist=["async_session_factory"])
            monkeypatch.setattr(
                mod, "async_session_factory", factory, raising=False,
            )
        except Exception:
            pass

    # Reset every federation singleton — they cache state between tests.
    import app.services.federation_v2.peer_manager as _pm
    import app.services.federation_v2.shaper as _sh
    import app.services.federation_v2.policy_engine as _pe
    import app.services.federation_v2.cert_manager as _cm
    import app.services.federation_v2.quorum as _qm
    import app.services.federation_v2.replication_monitor as _rm
    import app.services.federation_v2.diagnostics as _dx
    import app.services.federation_v2.ws_stream as _ws
    _pm._manager = None
    _sh._shaper = None
    _pe._engine = None
    _cm._mgr = None
    _qm._quorum = None
    _rm._monitor = None
    _dx._diag = None
    _ws._mgr = None

    app = create_app()

    async def _override_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db

    from httpx import ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, app


@pytest.fixture
async def seeded_peer(db_session):
    """Seed a single peer + meta row."""
    from app.models.federation_peer import FederationPeerMeta
    from app.models.federation_v2 import FederatedServer

    s = FederatedServer(
        server_id="peer-one.example.org",
        public_key="UFVCS0VZQkFTRTY0",
        advertise_url="https://peer-one.example.org",
        status="active",
        trust_level="peer",
        trust_score=0.5,
        version="7.0.0",
        signing_algo="ed25519",
        capabilities={"events.dag": True},
        last_seen=datetime.now(timezone.utc),
    )
    db_session.add(s)
    await db_session.flush()
    m = FederationPeerMeta(
        server_id="peer-one.example.org",
        hostname="peer-one",
        ip_address="10.0.0.5",
        region="us-east",
        role="follower",
        health_state="healthy",
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(s)
    return s.id, s.server_id


# ─────────────────────────────────────────────────────────────
# Auth gating
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peers_requires_auth(fed_client):
    ac, _ = fed_client
    r = await ac.get("/api/admin/federation/peers")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_peers_rejects_non_admin(fed_client):
    ac, _ = fed_client
    r = await ac.get("/api/admin/federation/peers", headers=_user_headers())
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_topology_requires_admin(fed_client):
    ac, _ = fed_client
    r = await ac.get("/api/admin/federation/topology")
    assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────
# Peer endpoints
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_peers_empty(fed_client):
    ac, _ = fed_client
    r = await ac.get("/api/admin/federation/peers", headers=_admin_headers())
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_peers_with_seed(fed_client, seeded_peer):
    ac, _ = fed_client
    _peer_id, server_id = seeded_peer
    r = await ac.get("/api/admin/federation/peers", headers=_admin_headers())
    assert r.status_code == 200
    data = r.json()
    assert any(p["server_id"] == server_id for p in data)


@pytest.mark.asyncio
async def test_get_peer_detail(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, server_id = seeded_peer
    r = await ac.get(
        f"/api/admin/federation/peers/{peer_id}",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["server_id"] == server_id
    assert "metrics_history" in data


@pytest.mark.asyncio
async def test_get_peer_404(fed_client):
    ac, _ = fed_client
    r = await ac.get(
        "/api/admin/federation/peers/missing",
        headers=_admin_headers(),
    )
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────
# Sync state
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_sync_state(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r = await ac.get(
        f"/api/admin/federation/peers/{peer_id}/sync-state",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "tables" in data and isinstance(data["tables"], dict)


# ─────────────────────────────────────────────────────────────
# Metrics + bandwidth
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_history(fed_client, seeded_peer):
    import time as _t
    from app.services.federation_v2.peer_manager import (
        MetricPoint, get_peer_manager,
    )
    ac, _ = fed_client
    peer_id, server_id = seeded_peer
    pm = get_peer_manager()
    for i in range(5):
        pm.record_metric(server_id, MetricPoint(
            ts=_t.time() - i, rtt_ms=20 + i, throughput_kbps=500 + i * 10,
        ))
    r = await ac.get(
        f"/api/admin/federation/peers/{peer_id}/metrics",
        headers=_admin_headers(),
        params={"range": 60},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["server_id"] == server_id
    assert len(data["points"]) >= 5


@pytest.mark.asyncio
async def test_get_bandwidth_no_rule(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r = await ac.get(
        f"/api/admin/federation/peers/{peer_id}/bandwidth",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "configured" in data and "actual" in data


@pytest.mark.asyncio
async def test_set_peer_shaper(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    body = {"in_kbps": 1000, "out_kbps": 2000, "burst_kbps": 500, "priority": 5}
    r = await ac.put(
        f"/api/admin/federation/peers/{peer_id}/shaper",
        json=body, headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    rule = r.json()
    assert rule["in_kbps"] == 1000
    assert rule["out_kbps"] == 2000
    assert rule["priority"] == 5


@pytest.mark.asyncio
async def test_shaper_priority_validation(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    body = {"in_kbps": 100, "out_kbps": 100, "priority": 99}
    r = await ac.put(
        f"/api/admin/federation/peers/{peer_id}/shaper",
        json=body, headers=_admin_headers(),
    )
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_shaper_bulk_equal(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.post(
        "/api/admin/federation/shaper/bulk",
        json={"preset": "equal", "params": {"total_in_kbps": 4000}},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["preset"] == "equal"
    assert len(data["rules"]) >= 1


@pytest.mark.asyncio
async def test_shaper_bulk_invalid_preset(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.post(
        "/api/admin/federation/shaper/bulk",
        json={"preset": "nope"},
        headers=_admin_headers(),
    )
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────
# Cert
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cert_info_pre_rotation(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, server_id = seeded_peer
    r = await ac.get(
        f"/api/admin/federation/peers/{peer_id}/cert",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("server_id") == server_id
    assert data.get("present") is False


@pytest.mark.asyncio
async def test_cert_rotate(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r = await ac.post(
        f"/api/admin/federation/peers/{peer_id}/cert/rotate",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["fingerprint_sha256"]


@pytest.mark.asyncio
async def test_cert_rotate_all(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.post(
        "/api/admin/federation/certs/rotate-all",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["count"] >= 1


# ─────────────────────────────────────────────────────────────
# Quarantine / role
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quarantine_then_release(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r1 = await ac.post(
        f"/api/admin/federation/peers/{peer_id}/quarantine",
        json={"reason": "operator-test"},
        headers=_admin_headers(),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["quarantined"] is True
    r2 = await ac.delete(
        f"/api/admin/federation/peers/{peer_id}/quarantine",
        headers=_admin_headers(),
    )
    assert r2.status_code == 200
    assert r2.json()["quarantined"] is False


@pytest.mark.asyncio
async def test_promote_demote(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r1 = await ac.post(
        f"/api/admin/federation/peers/{peer_id}/promote",
        json={"role": "master"},
        headers=_admin_headers(),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["role"] == "master"
    r2 = await ac.post(
        f"/api/admin/federation/peers/{peer_id}/demote",
        json={"role": "follower"},
        headers=_admin_headers(),
    )
    assert r2.status_code == 200
    assert r2.json()["role"] == "follower"


@pytest.mark.asyncio
async def test_promote_invalid_role(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r = await ac.post(
        f"/api/admin/federation/peers/{peer_id}/promote",
        json={"role": "godking"},
        headers=_admin_headers(),
    )
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────
# Audit / diagnose
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peer_audit_after_quarantine(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    await ac.post(
        f"/api/admin/federation/peers/{peer_id}/quarantine",
        json={"reason": "audit-test"},
        headers=_admin_headers(),
    )
    r = await ac.get(
        f"/api/admin/federation/peers/{peer_id}/audit",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert any(row["category"] == "admin" for row in rows)


@pytest.mark.asyncio
async def test_diagnose_unreachable_peer(fed_client, seeded_peer):
    ac, _ = fed_client
    peer_id, _ = seeded_peer
    r = await ac.post(
        f"/api/admin/federation/peers/{peer_id}/diagnose",
        json={"timeout_sec": 0.5},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "hops" in data
    assert any("hop" in h for h in data["hops"])


@pytest.mark.asyncio
async def test_diagnostics_skew(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.post(
        "/api/admin/federation/diagnostics/skew",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "peers" in data and "checked" in data


@pytest.mark.asyncio
async def test_diagnostics_cert_chain(fed_client, seeded_peer):
    ac, _ = fed_client
    _peer_id, server_id = seeded_peer
    r = await ac.post(
        "/api/admin/federation/diagnostics/cert-chain",
        json={"peer_id": server_id},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "server_id" in data


# ─────────────────────────────────────────────────────────────
# Topology + replication
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topology(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.get(
        "/api/admin/federation/topology",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "edges" in data
    assert data["count"] >= 1


@pytest.mark.asyncio
async def test_replication_lag(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.get(
        "/api/admin/federation/replication/lag",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "matrix" in data


# ─────────────────────────────────────────────────────────────
# Policies
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policies_crud(fed_client):
    ac, _ = fed_client
    r0 = await ac.get(
        "/api/admin/federation/policies", headers=_admin_headers(),
    )
    assert r0.status_code == 200
    assert isinstance(r0.json(), list)

    body = {
        "name": "block-untrusted-edits",
        "priority": 10,
        "match": {"kind": "edit", "min_trust": "peer"},
        "action": {"route_to": ["peer-one.example.org"], "blackhole": False},
    }
    r1 = await ac.post(
        "/api/admin/federation/policies",
        json=body, headers=_admin_headers(),
    )
    assert r1.status_code == 201, r1.text
    pid = r1.json()["id"]

    r2 = await ac.get(
        "/api/admin/federation/policies", headers=_admin_headers(),
    )
    names = [p["name"] for p in r2.json()]
    assert "block-untrusted-edits" in names

    r3 = await ac.delete(
        f"/api/admin/federation/policies/{pid}",
        headers=_admin_headers(),
    )
    assert r3.status_code == 200

    r4 = await ac.delete(
        "/api/admin/federation/policies/does-not-exist",
        headers=_admin_headers(),
    )
    assert r4.status_code == 404


@pytest.mark.asyncio
async def test_policy_simulate(fed_client, seeded_peer):
    ac, _ = fed_client
    body = {
        "envelope": {
            "kind":    "message",
            "channel": "#general@local",
            "sender":  "user@local",
            "origin":  "peer-one.example.org",
        }
    }
    r = await ac.post(
        "/api/admin/federation/policies/simulate",
        json=body, headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "route_to" in data and "reason" in data


# ─────────────────────────────────────────────────────────────
# Quorum
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quorum_state_graceful_degradation(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.get(
        "/api/admin/federation/quorum", headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "state" in data and "members" in data and "split_brain" in data
    assert data["state"]["enabled"] is False


@pytest.mark.asyncio
async def test_quorum_force_election_synthetic(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.post(
        "/api/admin/federation/quorum/election",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True


# ─────────────────────────────────────────────────────────────
# Global sync
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_sync(fed_client, seeded_peer):
    ac, _ = fed_client
    r = await ac.post(
        "/api/admin/federation/sync", headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True


# ─────────────────────────────────────────────────────────────
# WebSocket — uses sync TestClient because httpx AsyncClient doesn't
# do WS over ASGI.
# ─────────────────────────────────────────────────────────────


def _make_sync_ws_app(monkeypatch, db_factory):
    """Build a fresh FastAPI app with only the federation router for
    WS tests. Reuses a passed-in session factory."""
    from fastapi import FastAPI

    # Reset singletons
    import app.services.federation_v2.peer_manager as _pm
    import app.services.federation_v2.shaper as _sh
    import app.services.federation_v2.policy_engine as _pe
    import app.services.federation_v2.cert_manager as _cm
    import app.services.federation_v2.quorum as _qm
    import app.services.federation_v2.replication_monitor as _rm
    import app.services.federation_v2.diagnostics as _dx
    import app.services.federation_v2.ws_stream as _ws
    _pm._manager = None
    _sh._shaper = None
    _pe._engine = None
    _cm._mgr = None
    _qm._quorum = None
    _rm._monitor = None
    _dx._diag = None
    _ws._mgr = None

    import app.db.session as _session
    for mod_path in (
        "app.db.session",
        "app.services.federation_v2.peer_manager",
        "app.services.federation_v2.replication_monitor",
        "app.services.federation_v2.shaper",
        "app.services.federation_v2.cert_manager",
        "app.services.federation_v2.quorum",
        "app.services.federation_v2.policy_engine",
        "app.services.federation_v2.diagnostics",
    ):
        mod = __import__(mod_path, fromlist=["async_session_factory"])
        monkeypatch.setattr(
            mod, "async_session_factory", db_factory, raising=False,
        )

    from app.api.routes.admin_federation import router as fed_router
    app = FastAPI()
    app.include_router(fed_router)
    return app


def test_ws_requires_token(monkeypatch, tmp_path):
    """Token-less WS — must close immediately (TestClient surfaces this
    as a ``WebSocketDisconnect``)."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool
    import app.models  # noqa: F401 — register tables
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = _make_sync_ws_app(monkeypatch, factory)
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/api/admin/federation/ws/federation"):
                pass


def test_ws_non_admin_rejected(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool
    import app.models  # noqa: F401
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = _make_sync_ws_app(monkeypatch, factory)
    token = create_access_token("u", role="user")
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect(
                f"/api/admin/federation/ws/federation?token={token}",
            ):
                pass
