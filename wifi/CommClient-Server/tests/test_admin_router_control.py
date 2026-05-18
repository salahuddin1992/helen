"""
Tests for the Helen-Router admin proxy router.

Coverage matrix
---------------
  * **Per-endpoint smoke**: every forward route returns the proxied
    body when the upstream is healthy.
  * **Auth**:
      - 401 when no token presented.
      - 403 when a non-admin token presented.
      - 200 with admin token.
  * **502 path**: when the upstream is unreachable (ConnectError on
    every retry) the route surfaces a 502 with a structured body.
  * **Token swap**: the inbound ``Authorization`` header is *never*
    forwarded; it is replaced with the configured router bearer
    token. We assert the bytes that hit the upstream.
  * **Audit emission**: a successful write op produces at least one
    matching audit entry (attempt + result).
  * **Connection config**: the local control endpoint never echoes
    the token but does flip ``token_set`` correctly.

We replace the global ``HelenRouterClient``'s underlying
``httpx.AsyncClient`` with a custom one wired to a
:class:`httpx.MockTransport`. That keeps the tests fully offline
and lets each test record/replay the exact upstream interaction.

Avoiding real DB: we don't exercise the override row in tests —
the env-var fallback is sufficient for the proxy's behaviour. The
shaded config store still works on the in-memory test DB because
the model imports succeed; the override path is exercised in a
dedicated test.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from app.core.deps import get_db
from app.core.security import create_access_token
from app.db.base import Base
from app.main import create_app
from app.services.router_control import (
    get_router_client,
    get_router_config_store,
)


# =====================================================================
# Test scaffolding
# =====================================================================


class _UpstreamRecorder:
    """Records every request the proxy forwards and lets each test
    queue a response. The shape (status, headers, body) is just like
    a real httpx round-trip but resolved entirely in-process."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self._responses: list[httpx.Response | Exception] = []

    def enqueue(
        self,
        status_code: int = 200,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        body = (raw_body if raw_body is not None
                else json.dumps(json_body or {}).encode("utf-8"))
        out_headers = {"content-type": "application/json"}
        if headers:
            out_headers.update(headers)
        self._responses.append(httpx.Response(
            status_code=status_code,
            headers=out_headers,
            content=body,
        ))

    def enqueue_error(self, exc: Exception) -> None:
        self._responses.append(exc)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self._responses:
            # Default: 200 OK empty body so tests that don't queue
            # anything still pass the basic 200 smoke check.
            return httpx.Response(200, json={"ok": True})
        rsp = self._responses.pop(0)
        if isinstance(rsp, Exception):
            raise rsp
        return rsp


@pytest.fixture
async def proxy_app(monkeypatch):
    """Spin up Helen-Server with an in-memory DB and a router client
    whose underlying httpx instance is bound to a MockTransport.

    Returns a tuple of (client, admin_token, recorder).
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )
    from sqlalchemy.pool import StaticPool

    # ── In-memory DB so the real app boots ──────────────────
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )

    async def override_get_db():
        async with Session() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    # ── Configure env-var fallback in the config store ──────
    monkeypatch.setenv("HELEN_ROUTER_BASE_URL", "http://router.test.lan:8080")
    monkeypatch.setenv("HELEN_ROUTER_TOKEN", "test-router-token-32chars-abcdefghi")

    # Invalidate cache so the new env vars are picked up
    await get_router_config_store().invalidate()

    # ── Hook the global router client onto a MockTransport ─
    recorder = _UpstreamRecorder()
    mock_transport = httpx.MockTransport(recorder.handler)

    client = await get_router_client()
    # Force-reset the client's internal AsyncClient so we can
    # swap in the mock transport.
    await client.aclose()
    # Create a fresh AsyncClient using the mock transport, then
    # stash it onto the singleton's slot. The singleton's
    # _ensure_client() will see is_closed=False and use it.
    fake = httpx.AsyncClient(transport=mock_transport)
    client._client = fake  # noqa: SLF001 — test-only injection

    admin_token = create_access_token("admin-uid", role="admin")
    user_token = create_access_token("user-uid", role="user")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, admin_token, user_token, recorder

    await fake.aclose()
    client._client = None  # noqa: SLF001
    await engine.dispose()


# =====================================================================
# Auth
# =====================================================================


@pytest.mark.asyncio
async def test_missing_token_returns_401(proxy_app):
    ac, _, _, _ = proxy_app
    r = await ac.get("/api/admin/router/health")
    # The shared HTTPBearer dependency returns 403 when no Authorization
    # header is provided (FastAPI default) — both 401/403 are acceptable
    # for the "not authenticated" branch.
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_non_admin_returns_403(proxy_app):
    ac, _, user_token, _ = proxy_app
    r = await ac.get(
        "/api/admin/router/health",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 403


# =====================================================================
# Forward path
# =====================================================================


@pytest.mark.asyncio
async def test_get_router_health_proxies_body(proxy_app):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={
        "status": "ok", "service": "helen-router",
    })
    r = await ac.get(
        "/api/admin/router/health",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "helen-router"

    # Latency diagnostics surface in the response headers.
    assert "x-router-latency-ms" in {h.lower() for h in r.headers.keys()}


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", [
    ("/api/admin/router/upstreams", "GET"),
    ("/api/admin/mesh/topology", "GET"),
    ("/api/admin/router/dns/records", "GET"),
    ("/api/admin/router/dns/blocklist", "GET"),
    ("/api/admin/router/dns/stats", "GET"),
    ("/api/admin/router/dns/upstreams", "GET"),
    ("/api/admin/router/ntp/status", "GET"),
    ("/api/admin/router/upnp/portmaps", "GET"),
    ("/api/admin/router/external", "GET"),
    ("/api/admin/router/broker/status", "GET"),
    ("/api/admin/router/security", "GET"),
    ("/api/admin/router/security/subnets", "GET"),
    ("/api/admin/router/proxy/rate-limits", "GET"),
    ("/api/admin/router/proxy/ip-lists/blocklist", "GET"),
    ("/api/admin/router/vendor/jobs", "GET"),
    ("/api/admin/router/config", "GET"),
    ("/api/admin/router/routing/rules", "GET"),
    ("/api/admin/router/register", "GET"),
    ("/api/admin/mesh/neighbours", "GET"),
])
async def test_each_get_route_round_trips(proxy_app, path, method):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"forwarded": True, "path": path})
    r = await ac.get(path, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["forwarded"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method,body", [
    ("/api/admin/router/register", "POST", {"server_id": "s1", "url": "http://x"}),
    ("/api/admin/router/register/s1", "PUT", {"url": "http://x"}),
    ("/api/admin/router/register/s1", "DELETE", None),
    ("/api/admin/mesh/reroute", "POST", {"server_id": "s1"}),
    ("/api/admin/mesh/neighbours/n1", "DELETE", None),
    ("/api/admin/router/proxy/rate-limits", "POST", {"rps": 10}),
    ("/api/admin/router/proxy/ip-lists/deny", "POST", {"ips": ["1.2.3.4"]}),
    ("/api/admin/router/dns/records", "POST", {"name": "a.lan", "type": "A", "value": "10.0.0.1"}),
    ("/api/admin/router/dns/records", "DELETE", None),
    ("/api/admin/router/dns/blocklist", "POST", {"add": ["bad.lan"]}),
    ("/api/admin/router/dns/upstreams", "POST", {"upstreams": ["1.1.1.1"]}),
    ("/api/admin/router/ntp/sync", "POST", None),
    ("/api/admin/router/upnp/portmap", "POST", {"external_port": 8080}),
    ("/api/admin/router/upnp/portmap/m1", "DELETE", None),
    ("/api/admin/router/upnp/discover", "POST", None),
    ("/api/admin/router/vendor/test", "POST", {"vendor": "asus"}),
    ("/api/admin/router/vendor/push", "POST", {"vendor": "asus", "config": {}}),
    ("/api/admin/router/external/scan", "POST", None),
    ("/api/admin/router/security/rotate-token", "POST", None),
    ("/api/admin/router/security/subnets", "POST", {"allow": ["10.0.0.0/8"]}),
    ("/api/admin/router/security/enforcement", "POST", {"mode": "enforce"}),
    ("/api/admin/router/diag/ping", "POST", {"target": "8.8.8.8"}),
    ("/api/admin/router/diag/traceroute", "POST", {"target": "8.8.8.8"}),
    ("/api/admin/router/diag/dns", "POST", {"name": "lan"}),
    ("/api/admin/router/diag/portscan", "POST", {"target": "10.0.0.1"}),
    ("/api/admin/router/diag/bandwidth", "POST", {"peer": "10.0.0.2"}),
    ("/api/admin/router/config", "PUT", {"some": "value"}),
    ("/api/admin/router/config/validate", "POST", {"some": "value"}),
    ("/api/admin/router/admin/reload", "POST", None),
    ("/api/admin/router/admin/restart", "POST", None),
    ("/api/admin/router/routing/rules", "POST", {"rule": {}}),
    ("/api/admin/router/routing/rules", "DELETE", None),
])
async def test_each_write_route_round_trips(proxy_app, path, method, body):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"ok": True})
    req = getattr(ac, method.lower())
    headers = {"Authorization": f"Bearer {admin_token}"}
    r = await req(path, json=body, headers=headers) if body is not None \
        else await req(path, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


# =====================================================================
# 502 — router unreachable
# =====================================================================


@pytest.mark.asyncio
async def test_router_unreachable_returns_502(proxy_app):
    ac, admin_token, _, rec = proxy_app

    # Queue ConnectError for every retry. Default retry budget is
    # 3 retries = 4 total attempts.
    for _ in range(10):
        rec.enqueue_error(
            httpx.ConnectError(
                "connection refused", request=httpx.Request(
                    "GET", "http://router.test.lan:8080/router/health",
                ),
            ),
        )
    r = await ac.get(
        "/api/admin/router/health",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "router_unreachable"
    assert "detail" in body
    assert body["attempts"] >= 1


# =====================================================================
# Token swap — outbound Authorization is always router token
# =====================================================================


@pytest.mark.asyncio
async def test_outbound_authorization_uses_router_token(proxy_app):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"ok": True})
    r = await ac.get(
        "/api/admin/router/health",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert len(rec.calls) == 1
    outbound = rec.calls[0].headers.get("authorization", "")
    assert outbound.startswith("Bearer ")
    # CRITICAL: the admin's JWT must NOT survive to the upstream.
    assert admin_token not in outbound
    # And the router token MUST be present.
    assert outbound == "Bearer test-router-token-32chars-abcdefghi"


@pytest.mark.asyncio
async def test_outbound_request_keeps_query_and_body(proxy_app):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"ok": True})
    r = await ac.post(
        "/api/admin/router/diag/ping?count=3",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"target": "10.0.0.1"},
    )
    assert r.status_code == 200
    assert len(rec.calls) == 1
    fwd = rec.calls[0]
    assert "count=3" in str(fwd.url)
    body = json.loads(fwd.content.decode("utf-8"))
    assert body["target"] == "10.0.0.1"


# =====================================================================
# Audit — write op fires audit_log with the expected event
# =====================================================================


@pytest.mark.asyncio
async def test_write_op_emits_audit_entries(proxy_app, monkeypatch):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"status": "registered"})

    seen: list[dict[str, Any]] = []

    def _capture(event, *, user_id=None, ip_address=None,
                 success=True, details=None):
        seen.append({
            "event": event, "user_id": user_id,
            "success": success, "details": details or {},
        })

    monkeypatch.setattr(
        "app.services.router_control.audit_hook.audit_log",
        _capture,
    )

    r = await ac.post(
        "/api/admin/router/register",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"server_id": "s1", "url": "http://upstream"},
    )
    assert r.status_code == 200

    events = [e["event"] for e in seen]
    # Expect the before/after pair for the router.registry.register op.
    assert any(e == "router.registry.register.attempt" for e in events)
    assert any(e == "router.registry.register.result" for e in events)

    # The "result" entry should mark success and carry status_code=200.
    result = next(e for e in seen
                  if e["event"] == "router.registry.register.result")
    assert result["success"] is True
    assert result["details"].get("status_code") == 200


@pytest.mark.asyncio
async def test_audit_redacts_token_in_body(proxy_app, monkeypatch):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"ok": True})

    seen: list[dict[str, Any]] = []

    def _capture(event, *, user_id=None, ip_address=None,
                 success=True, details=None):
        seen.append({"event": event, "details": details or {}})

    monkeypatch.setattr(
        "app.services.router_control.audit_hook.audit_log",
        _capture,
    )

    await ac.put(
        "/api/admin/router/config",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"token": "super-secret-value", "other": "kept"},
    )
    attempt = next(e for e in seen
                   if e["event"].endswith(".attempt"))
    body = attempt["details"]["body"]
    assert body["token"] == "***REDACTED***"
    assert body["other"] == "kept"


# =====================================================================
# Local control endpoints — never echo the token, flip token_set
# =====================================================================


@pytest.mark.asyncio
async def test_get_connection_config_never_echoes_token(proxy_app):
    ac, admin_token, _, _ = proxy_app
    r = await ac.get(
        "/api/admin/router/control/connection",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_set"] is True
    assert "token" not in body  # The actual bytes are never returned.
    assert body["base_url"].startswith("http://router.test.lan")


@pytest.mark.asyncio
async def test_reachability_probe_returns_bool(proxy_app):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"status": "ok"})
    r = await ac.get(
        "/api/admin/router/reachability",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["reachable"], bool)
    assert "base_url" in body
    assert body["source"] in ("db", "env", "default")


# =====================================================================
# Header hygiene — hop-by-hop and Cookie are stripped on the way out
# =====================================================================


@pytest.mark.asyncio
async def test_cookies_not_forwarded(proxy_app):
    ac, admin_token, _, rec = proxy_app
    rec.enqueue(status_code=200, json_body={"ok": True})
    r = await ac.get(
        "/api/admin/router/health",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Cookie": "session=abc; tracker=xyz",
        },
    )
    assert r.status_code == 200
    fwd = rec.calls[0]
    assert "cookie" not in {k.lower() for k in fwd.headers.keys()}


# =====================================================================
# Streaming endpoint — log dump
# =====================================================================


@pytest.mark.asyncio
async def test_proxy_log_streams_response(proxy_app):
    ac, admin_token, _, rec = proxy_app
    # Simulate a large textual log dump.
    body = ("\n".join(f"line-{i}" for i in range(1000)) + "\n").encode()
    rec.enqueue(
        status_code=200,
        raw_body=body,
        headers={"content-type": "text/plain"},
    )
    r = await ac.get(
        "/api/admin/router/proxy/log",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.content.startswith(b"line-0\n")
    assert r.content.endswith(b"line-999\n")
