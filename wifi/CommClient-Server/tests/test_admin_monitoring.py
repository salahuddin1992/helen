"""
Tests for the admin monitoring router and supporting services.

These tests build a lightweight FastAPI app that mounts ONLY the new
``admin_monitoring`` router. That isolates them from the full Helen app
boot sequence (which is heavyweight) while still exercising the real
router + dependency wiring end-to-end.

Auth strategy
-------------
Most tests inject a synthetic admin Bearer token via the project's
``create_access_token(..., role="admin")`` helper, so we exercise the
real RBAC dependency rather than a stub. The unauthenticated test omits
the header entirely.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── App factory under test ────────────────────────────────────────────────


def _make_app() -> FastAPI:
    from app.api.routes.admin_monitoring import router as monitoring_router
    app = FastAPI()
    app.include_router(monitoring_router)
    return app


def _admin_headers() -> dict[str, str]:
    from app.core.security import create_access_token
    tok = create_access_token("admin-test-uid", role="admin")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def client():
    app = _make_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def headers():
    return _admin_headers()


# ── /observability/metrics ────────────────────────────────────────────────


def test_observability_metrics_returns_required_keys(client, headers):
    r = client.get("/api/admin/observability/metrics", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    for key in (
        "cpu", "mem", "net_in_mbps", "net_out_mbps", "disk_io_mbps",
        "rps", "errors", "rtt_ms", "alerts", "ts",
    ):
        assert key in data, f"missing key {key!r} in {data}"
    assert isinstance(data["alerts"], list)
    assert 0 <= data["cpu"] <= 100
    assert 0 <= data["mem"] <= 100


def test_observability_metrics_unauthenticated_is_401_or_403(client):
    r = client.get("/api/admin/observability/metrics")
    assert r.status_code in (401, 403)


def test_observability_metrics_wrong_role_is_403(client):
    from app.core.security import create_access_token
    tok = create_access_token("non-admin", role="user")
    r = client.get(
        "/api/admin/observability/metrics",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403


# ── /transports/{name}/status ─────────────────────────────────────────────


def test_transport_status_nats(client, headers):
    r = client.get("/api/admin/transports/nats/status", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "nats"
    assert data["status"] in ("healthy", "degraded", "down")
    for key in ("msg_per_sec", "conn_count", "latency_p50_ms", "latency_p99_ms", "tags"):
        assert key in data


@pytest.mark.parametrize("name", ["mqtt", "zeromq", "rabbitmq", "grpc", "wireguard", "ssh"])
def test_transport_status_each_supported(client, headers, name):
    r = client.get(f"/api/admin/transports/{name}/status", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name


def test_transport_status_unknown_is_404(client, headers):
    r = client.get("/api/admin/transports/no-such-transport/status", headers=headers)
    assert r.status_code == 404


# ── /connections/list ─────────────────────────────────────────────────────


def _seed_connections(n: int = 12) -> list[str]:
    from app.services.monitoring.connection_registry import (
        ConnectionInfo, get_connection_registry,
    )
    reg = get_connection_registry()
    ids: list[str] = []

    async def seed():
        # Clean slate
        for cid in list(reg._conns.keys()):  # noqa: SLF001 — test setup
            await reg.unregister(cid)
        for i in range(n):
            cid = f"conn-{i}"
            ids.append(cid)
            await reg.register(ConnectionInfo(
                id=cid,
                user_id=f"user-{i}",
                username=f"user_{i}",
                ip=f"10.0.0.{i}",
                transport=("nats" if i % 2 else "mqtt"),
                connected_at=time.time() - (n - i),
            ))

    asyncio.run(seed())
    return ids


def test_connections_list_default(client, headers):
    _seed_connections(12)
    r = client.get("/api/admin/connections/list", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 12
    assert len(data["items"]) == 12
    # Newest first
    assert data["items"][0]["id"] == "conn-11"


def test_connections_list_pagination(client, headers):
    _seed_connections(12)
    r = client.get(
        "/api/admin/connections/list?limit=5&offset=0", headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 5
    assert data["limit"] == 5

    r2 = client.get(
        "/api/admin/connections/list?limit=5&offset=5", headers=headers,
    )
    data2 = r2.json()
    assert len(data2["items"]) == 5
    assert {x["id"] for x in data["items"]}.isdisjoint(
        {x["id"] for x in data2["items"]}
    )


def test_connections_list_filter_by_transport(client, headers):
    _seed_connections(12)
    r = client.get(
        "/api/admin/connections/list?transport=nats", headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert all(c["transport"] == "nats" for c in data["items"])


def test_connections_list_search(client, headers):
    _seed_connections(12)
    r = client.get(
        "/api/admin/connections/list?search=user_3", headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert any("user_3" in c["username"] for c in data["items"])


# ── /connections/{conn_id}/kick ───────────────────────────────────────────


def test_kick_existing_connection(client, headers):
    ids = _seed_connections(3)
    target = ids[0]
    r = client.post(f"/api/admin/connections/{target}/kick", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["conn_id"] == target

    # Second kick of the same id should now 404
    r2 = client.post(f"/api/admin/connections/{target}/kick", headers=headers)
    assert r2.status_code == 404


def test_kick_unknown_connection_is_404(client, headers):
    _seed_connections(2)
    r = client.post("/api/admin/connections/does-not-exist/kick", headers=headers)
    assert r.status_code == 404


def test_legacy_disconnect_alias(client, headers):
    ids = _seed_connections(2)
    target = ids[1]
    r = client.post(f"/api/admin/clients/{target}/disconnect", headers=headers)
    assert r.status_code == 200
    assert r.json()["client_id"] == target


# ── /ws/metrics ───────────────────────────────────────────────────────────


def test_ws_metrics_rejects_unauthenticated(client):
    import websockets  # noqa: F401  — ensure dep is present
    from starlette.websockets import WebSocketDisconnect
    try:
        with client.websocket_connect("/api/admin/ws/metrics") as ws:
            ws.receive_text()
            pytest.fail("connection should have been rejected")
    except Exception:
        # TestClient raises on 4401 close — that's the expected branch
        pass


def test_ws_metrics_first_frame_is_metric(client):
    from app.core.security import create_access_token
    tok = create_access_token("admin-ws-uid", role="admin")
    with client.websocket_connect(f"/api/admin/ws/metrics?token={tok}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "metric"
        assert "metrics" in msg
        for key in ("cpu", "mem", "rps", "errors", "alerts"):
            assert key in msg["metrics"]


# ── ConnectionRegistry unit ───────────────────────────────────────────────


def test_connection_registry_traffic_updates():
    from app.services.monitoring.connection_registry import (
        ConnectionInfo, get_connection_registry,
    )
    reg = get_connection_registry()

    async def go() -> dict[str, Any]:
        await reg.register(ConnectionInfo(
            id="t-traffic", user_id="u", username="u", ip="1.1.1.1",
            transport="nats", connected_at=time.time(),
        ))
        await reg.update_traffic("t-traffic", bytes_in=100, bytes_out=50)
        await reg.update_traffic("t-traffic", bytes_in=25, bytes_out=10)
        info = await reg.get("t-traffic")
        await reg.unregister("t-traffic")
        return info.to_dict() if info else {}

    result = asyncio.run(go())
    assert result.get("bytes_in") == 125
    assert result.get("bytes_out") == 60
