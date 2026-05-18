"""
Tests for the cross-server call lifecycle RPC endpoint
(`POST /api/federation/call/rpc`) plus the auth_refresh socket event.

The RPC endpoint runs accept/reject/leave/hangup/reinvite on behalf of
a sibling Helen server when the calling user lives on this server but
the authoritative ActiveCall is local. We exercise the happy paths and
the major error branches with HMAC-signed requests.
"""

from __future__ import annotations

import json
import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.federation_auth import (
    HEADER_ORIGIN,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    sign_request,
)
from app.core.security import create_access_token, create_refresh_token
from app.services.call_service import call_service


def _signed_post_headers(path: str, body: dict) -> dict:
    """Helper: produce the signed headers for a federation POST."""
    raw = json.dumps(body).encode("utf-8")
    headers = sign_request("POST", path, raw)
    headers[HEADER_ORIGIN] = "test-origin-server"
    headers["Content-Type"] = "application/json"
    return headers


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_federation(monkeypatch):
    """Make sure the federation endpoint is enabled for every test in
    this module — without this, /api/federation/call/rpc returns 403."""
    settings = get_settings()
    monkeypatch.setattr(settings, "FEDERATION_ENABLED", True)
    if not settings.FEDERATION_SECRET or len(settings.FEDERATION_SECRET) < 32:
        monkeypatch.setattr(
            settings, "FEDERATION_SECRET",
            "a" * 64,  # 64-byte test secret
        )


@pytest.fixture(autouse=True)
def _clean_call_service():
    """Prevent state bleeding between tests — the singleton call_service
    keeps both a call table and a user→call map, so we wipe both before
    and after each test in this module."""
    call_service._active_calls.clear()
    call_service._user_calls.clear()
    yield
    call_service._active_calls.clear()
    call_service._user_calls.clear()


# ── Authz: missing signature ──────────────────────────────────────


@pytest.mark.asyncio
async def test_unsigned_request_rejected(client: AsyncClient):
    """No HMAC headers → 401."""
    resp = await client.post(
        "/api/federation/call/rpc",
        json={"rpc": "accept", "call_id": "x", "user_id": "u"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bad_signature_rejected(client: AsyncClient):
    """Tampered signature → 401."""
    body = {"rpc": "accept", "call_id": "x", "user_id": "u"}
    raw = json.dumps(body).encode()
    headers = sign_request("POST", "/api/federation/call/rpc", raw)
    headers[HEADER_SIGNATURE] = "deadbeef" * 8  # garbage
    headers["Content-Type"] = "application/json"
    resp = await client.post(
        "/api/federation/call/rpc",
        content=raw,
        headers=headers,
    )
    assert resp.status_code == 401


# ── Body validation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_fields_rejected(client: AsyncClient):
    body = {"rpc": "accept"}  # call_id + user_id missing
    headers = _signed_post_headers("/api/federation/call/rpc", body)
    resp = await client.post(
        "/api/federation/call/rpc",
        content=json.dumps(body).encode(),
        headers=headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_call_not_found_returns_ok_false(client: AsyncClient):
    """Unknown call_id → 200 with ok:false (NOT a 4xx; the caller
    distinguishes via the body so transport-level retries don't fire)."""
    body = {
        "rpc": "accept",
        "call_id": "no-such-call",
        "user_id": "u",
    }
    headers = _signed_post_headers("/api/federation/call/rpc", body)
    resp = await client.post(
        "/api/federation/call/rpc",
        content=json.dumps(body).encode(),
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "call_not_found"


@pytest.mark.asyncio
async def test_unknown_rpc_returns_ok_false(client: AsyncClient):
    """Made-up rpc name → ok:false, NOT a 500."""
    # First create a real call so we get past the "call_not_found" gate.
    call = await call_service.initiate_call(
        initiator_id="alice", call_type="audio", routing="p2p",
    )
    call.participants["bob"] = {"joined_at": None, "muted": False}

    body = {
        "rpc": "fly-away",  # nonsense
        "call_id": call.call_id,
        "user_id": "bob",
    }
    headers = _signed_post_headers("/api/federation/call/rpc", body)
    resp = await client.post(
        "/api/federation/call/rpc",
        content=json.dumps(body).encode(),
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "unknown_rpc" in data["error"]


# ── Reject RPC ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_rpc_runs_origin_reject(client: AsyncClient):
    """Reject RPC: clear authz + persist call log."""
    call = await call_service.initiate_call(
        initiator_id="alice", call_type="audio", routing="p2p",
    )
    call.participants["bob"] = {"joined_at": None, "muted": False}

    body = {
        "rpc": "reject",
        "call_id": call.call_id,
        "user_id": "bob",
    }
    headers = _signed_post_headers("/api/federation/call/rpc", body)
    resp = await client.post(
        "/api/federation/call/rpc",
        content=json.dumps(body).encode(),
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["status"] == "rejected"


# ── Hangup RPC ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hangup_rpc_ends_call(client: AsyncClient):
    call = await call_service.initiate_call(
        initiator_id="alice", call_type="audio", routing="p2p",
    )
    await call_service.accept_call(call.call_id, "bob")

    body = {
        "rpc": "hangup",
        "call_id": call.call_id,
        "user_id": "alice",
    }
    headers = _signed_post_headers("/api/federation/call/rpc", body)
    resp = await client.post(
        "/api/federation/call/rpc",
        content=json.dumps(body).encode(),
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["status"] == "ended"


# ── Reinvite RPC ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reinvite_rpc_only_host(client: AsyncClient):
    """Non-host user_id → forbidden_only_host."""
    call = await call_service.initiate_call(
        initiator_id="alice", call_type="audio", routing="p2p",
    )
    body = {
        "rpc": "reinvite",
        "call_id": call.call_id,
        "user_id": "bob",  # not the host
        "extra": {"target_user_id": "carol"},
    }
    headers = _signed_post_headers("/api/federation/call/rpc", body)
    resp = await client.post(
        "/api/federation/call/rpc",
        content=json.dumps(body).encode(),
        headers=headers,
    )
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "forbidden_only_host"


# ── auth_refresh socket event ─────────────────────────────────────
#
# We test the handler logic directly rather than spinning up a real
# Socket.IO server — the handler is a plain async function once we
# patch get_user_id and sio.session.


class TestAuthRefresh:

    @pytest.mark.asyncio
    async def test_refresh_with_valid_refresh_token(self, monkeypatch):
        from app.socket import auth_handlers

        async def _fake_get_user_id(sid):
            return "alice"
        monkeypatch.setattr(auth_handlers, "get_user_id", _fake_get_user_id)

        class _FakeSession:
            def __init__(self): self.data = {"role": "user"}
            async def __aenter__(self): return self.data
            async def __aexit__(self, *a): pass

        class _FakeSio:
            def session(self, sid): return _FakeSession()
        monkeypatch.setattr(auth_handlers, "sio", _FakeSio())

        refresh = create_refresh_token("alice")
        result = await auth_handlers.auth_refresh("sid-1", {"refresh_token": refresh})
        assert result["ok"] is True
        assert "access_token" in result
        assert isinstance(result["access_token"], str)
        assert result.get("expires_in") and result["expires_in"] > 0

    @pytest.mark.asyncio
    async def test_refresh_with_user_mismatch(self, monkeypatch):
        from app.socket import auth_handlers

        async def _fake_get_user_id(sid):
            return "alice"
        monkeypatch.setattr(auth_handlers, "get_user_id", _fake_get_user_id)

        # Different user's refresh token — must reject.
        bobs_refresh = create_refresh_token("bob")
        result = await auth_handlers.auth_refresh("sid-1", {"refresh_token": bobs_refresh})
        assert result["ok"] is False
        assert result["error"] == "user_mismatch"

    @pytest.mark.asyncio
    async def test_refresh_with_access_token_rejected(self, monkeypatch):
        """Using an *access* token where a refresh token is expected
        must fail (type mismatch)."""
        from app.socket import auth_handlers

        async def _fake_get_user_id(sid):
            return "alice"
        monkeypatch.setattr(auth_handlers, "get_user_id", _fake_get_user_id)

        access = create_access_token("alice")
        result = await auth_handlers.auth_refresh("sid-1", {"refresh_token": access})
        assert result["ok"] is False
        assert result["error"] == "invalid_or_expired_refresh"

    @pytest.mark.asyncio
    async def test_refresh_with_no_session(self, monkeypatch):
        """No authenticated socket → no_session."""
        from app.socket import auth_handlers

        async def _fake_get_user_id(sid):
            return None
        monkeypatch.setattr(auth_handlers, "get_user_id", _fake_get_user_id)

        result = await auth_handlers.auth_refresh("sid-1", {"refresh_token": "x"})
        assert result["ok"] is False
        assert result["error"] == "no_session"

    @pytest.mark.asyncio
    async def test_refresh_with_oversize_token_rejected(self, monkeypatch):
        from app.socket import auth_handlers

        async def _fake_get_user_id(sid):
            return "alice"
        monkeypatch.setattr(auth_handlers, "get_user_id", _fake_get_user_id)

        result = await auth_handlers.auth_refresh(
            "sid-1", {"refresh_token": "x" * 5000},
        )
        assert result["ok"] is False
        assert result["error"] == "bad_token"
