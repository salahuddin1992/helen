"""
Tests for the new call moderation events:
  • call_kick_participant
  • call_force_mute
  • call_end_for_everyone

Validate authorization paths (host vs moderator vs random member),
state mutations on call_service, and authz-shadow eviction.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.call_service import call_service


@pytest.fixture(autouse=True)
def _clean_state():
    """Wipe in-memory call registry before/after each test."""
    call_service._active_calls.clear()
    call_service._user_calls.clear()
    yield
    call_service._active_calls.clear()
    call_service._user_calls.clear()


# ── _is_call_moderator ──────────────────────────────────────────────


class TestIsCallModerator:

    @pytest.mark.asyncio
    async def test_initiator_is_moderator(self):
        from app.socket.call_handlers import _is_call_moderator
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="p2p",
        )
        assert await _is_call_moderator(call, "alice") is True

    @pytest.mark.asyncio
    async def test_random_user_is_not_moderator(self):
        from app.socket.call_handlers import _is_call_moderator
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="p2p",
        )
        assert await _is_call_moderator(call, "bob") is False

    @pytest.mark.asyncio
    async def test_channel_admin_is_moderator(self, monkeypatch):
        from app.socket import call_handlers
        from app.services.channel_service import ChannelService
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio",
            routing="mesh", channel_id="ch1",
        )

        # Mock _get_member to return a member with role=admin
        mock_member = MagicMock(role="admin")
        async def fake_get_member(db, channel_id, user_id):
            return mock_member
        monkeypatch.setattr(ChannelService, "_get_member", fake_get_member)

        assert await call_handlers._is_call_moderator(call, "bob") is True

    @pytest.mark.asyncio
    async def test_member_role_is_not_moderator(self, monkeypatch):
        from app.socket import call_handlers
        from app.services.channel_service import ChannelService
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio",
            routing="mesh", channel_id="ch1",
        )

        mock_member = MagicMock(role="member")
        async def fake_get_member(db, channel_id, user_id):
            return mock_member
        monkeypatch.setattr(ChannelService, "_get_member", fake_get_member)

        assert await call_handlers._is_call_moderator(call, "bob") is False


# ── call_kick_participant ──────────────────────────────────────────


class TestCallKick:

    @pytest.mark.asyncio
    async def test_kick_unauthorized(self, monkeypatch):
        from app.socket import call_handlers
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")
        await call_service.join_group_call(call.call_id, "carol")

        # carol tries to kick bob — but carol is neither host nor moderator
        async def fake_get_user_id(sid):
            return "carol"

        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        # Mock channel_service to return non-moderator
        from app.services.channel_service import ChannelService
        async def fake_get_member(db, ch, uid):
            return MagicMock(role="member")
        monkeypatch.setattr(ChannelService, "_get_member", fake_get_member)

        result = await call_handlers.call_kick_participant("sid-carol", {
            "call_id": call.call_id,
            "target_user_id": "bob",
        })
        assert result == {"error": "forbidden"}
        # Bob still in call
        refreshed = call_service.get_call(call.call_id)
        assert "bob" in refreshed.participants

    @pytest.mark.asyncio
    async def test_kick_success_by_host(self, monkeypatch):
        from app.socket import call_handlers
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")

        async def fake_get_user_id(sid): return "alice"
        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        # Stub emit_to_user + sio.emit to no-ops so handler doesn't try
        # to talk to a real socket layer.
        monkeypatch.setattr(call_handlers, "emit_to_user", AsyncMock(return_value=1))
        monkeypatch.setattr(call_handlers.sio, "emit", AsyncMock())
        # Stub presence so it returns no remote-only sids work
        monkeypatch.setattr(
            call_handlers.presence_service, "get_sids",
            lambda uid: ["sid-" + uid] if uid else [],
        )

        result = await call_handlers.call_kick_participant("sid-alice", {
            "call_id": call.call_id,
            "target_user_id": "bob",
        })
        assert result.get("status") == "kicked"
        refreshed = call_service.get_call(call.call_id)
        # Either call ended (only host left) or bob no longer in it.
        if refreshed:
            assert "bob" not in refreshed.participants

    @pytest.mark.asyncio
    async def test_cannot_kick_host_as_non_host(self, monkeypatch):
        from app.socket import call_handlers
        from app.services.channel_service import ChannelService
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")

        # Bob is a moderator (not host)
        async def fake_get_user_id(sid): return "bob"
        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        async def fake_get_member(db, ch, uid):
            return MagicMock(role="moderator")
        monkeypatch.setattr(ChannelService, "_get_member", fake_get_member)

        result = await call_handlers.call_kick_participant("sid-bob", {
            "call_id": call.call_id,
            "target_user_id": "alice",  # the host
        })
        assert result.get("error") == "cannot_kick_host"


# ── call_force_mute ────────────────────────────────────────────────


class TestCallForceMute:

    @pytest.mark.asyncio
    async def test_force_mute_unauthorized(self, monkeypatch):
        from app.socket import call_handlers
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")
        await call_service.join_group_call(call.call_id, "carol")

        async def fake_get_user_id(sid): return "carol"
        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        from app.services.channel_service import ChannelService
        async def fake_get_member(db, ch, uid):
            return MagicMock(role="member")
        monkeypatch.setattr(ChannelService, "_get_member", fake_get_member)

        result = await call_handlers.call_force_mute("sid-c", {
            "call_id": call.call_id,
            "target_user_id": "bob",
            "muted": True,
        })
        assert result == {"error": "forbidden"}

    @pytest.mark.asyncio
    async def test_force_mute_by_host_succeeds(self, monkeypatch):
        from app.socket import call_handlers
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")

        async def fake_get_user_id(sid): return "alice"
        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        monkeypatch.setattr(call_handlers, "emit_to_user", AsyncMock(return_value=1))
        monkeypatch.setattr(call_handlers, "_broadcast_participant_state", AsyncMock())

        result = await call_handlers.call_force_mute("sid-a", {
            "call_id": call.call_id,
            "target_user_id": "bob",
            "muted": True,
        })
        assert result["status"] == "ok"
        assert result["muted"] is True


# ── call_end_for_everyone ──────────────────────────────────────────


class TestCallEndForEveryone:

    @pytest.mark.asyncio
    async def test_end_for_everyone_only_host(self, monkeypatch):
        from app.socket import call_handlers
        from app.services.channel_service import ChannelService
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")

        # Bob is a moderator — but END FOR EVERYONE is host-only.
        async def fake_get_user_id(sid): return "bob"
        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        async def fake_get_member(db, ch, uid):
            return MagicMock(role="moderator")
        monkeypatch.setattr(ChannelService, "_get_member", fake_get_member)

        result = await call_handlers.call_end_for_everyone("sid-b", {
            "call_id": call.call_id,
        })
        assert result == {"error": "forbidden_only_host"}

    @pytest.mark.asyncio
    async def test_end_for_everyone_by_host(self, monkeypatch):
        from app.socket import call_handlers
        call = await call_service.initiate_call(
            initiator_id="alice", call_type="audio", routing="mesh", channel_id="ch1",
        )
        await call_service.join_group_call(call.call_id, "bob")
        await call_service.join_group_call(call.call_id, "carol")

        async def fake_get_user_id(sid): return "alice"
        monkeypatch.setattr(call_handlers, "get_user_id", fake_get_user_id)
        monkeypatch.setattr(call_handlers, "emit_to_user", AsyncMock(return_value=1))
        # Stub persist_call_log so it doesn't hit the DB.
        monkeypatch.setattr(
            call_service, "persist_call_log", AsyncMock(),
        )

        result = await call_handlers.call_end_for_everyone("sid-a", {
            "call_id": call.call_id,
            "reason": "test_end",
        })
        assert result == {"status": "ended"}
        # Call should be gone or marked ended.
        assert call_service.get_call(call.call_id) is None or \
               call_service.get_call(call.call_id).status == "ended"
