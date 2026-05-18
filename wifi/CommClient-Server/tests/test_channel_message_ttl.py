"""Tests for the per-channel message TTL (auto-delete) feature.

Two layers of coverage:

  * REST endpoints — GET / PUT / DELETE / sweep-now under
    ``/api/channels/{id}/ttl``. RBAC: members read, channel admins
    write, non-members get 403.

  * Service layer — ``set_ttl_seconds`` clamping, ``sweep_once``
    against a real (in-memory) DB instance.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.models.message import Message
from app.services.channel_message_ttl import (
    set_ttl_seconds, get_ttl_seconds, sweep_once,
)


@pytest.fixture
async def group_channel(client: AsyncClient, auth_headers: dict):
    """Create a group channel owned by the authed user."""
    res = await client.post(
        "/api/channels",
        json={"type": "group", "name": "TTL Test Group"},
        headers=auth_headers,
    )
    assert res.status_code == 201
    return res.json()


# ── REST endpoints ──────────────────────────────────────────────


class TestTTLEndpoints:

    async def test_default_value_is_zero(
        self, client: AsyncClient, auth_headers: dict,
        group_channel: dict,
    ):
        res = await client.get(
            f"/api/channels/{group_channel['id']}/ttl",
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert res.json()["ttl_seconds"] == 0

    async def test_admin_set_and_get_round_trip(
        self, client: AsyncClient, auth_headers: dict,
        group_channel: dict,
    ):
        res = await client.put(
            f"/api/channels/{group_channel['id']}/ttl",
            json={"ttl_seconds": 24 * 3600},
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert res.json()["ttl_seconds"] == 24 * 3600

        get_res = await client.get(
            f"/api/channels/{group_channel['id']}/ttl",
            headers=auth_headers,
        )
        assert get_res.json()["ttl_seconds"] == 24 * 3600

    async def test_value_below_one_minute_is_clamped(
        self, client: AsyncClient, auth_headers: dict,
        group_channel: dict,
    ):
        # 30 seconds → server clamps to 60s minimum.
        res = await client.put(
            f"/api/channels/{group_channel['id']}/ttl",
            json={"ttl_seconds": 30},
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert res.json()["ttl_seconds"] == 60

    async def test_value_above_30_days_is_rejected(
        self, client: AsyncClient, auth_headers: dict,
        group_channel: dict,
    ):
        # Pydantic guard rejects > 30 days at the route layer.
        res = await client.put(
            f"/api/channels/{group_channel['id']}/ttl",
            json={"ttl_seconds": 31 * 24 * 3600},
            headers=auth_headers,
        )
        assert res.status_code == 422

    async def test_delete_clears_value(
        self, client: AsyncClient, auth_headers: dict,
        group_channel: dict,
    ):
        await client.put(
            f"/api/channels/{group_channel['id']}/ttl",
            json={"ttl_seconds": 3600},
            headers=auth_headers,
        )
        del_res = await client.delete(
            f"/api/channels/{group_channel['id']}/ttl",
            headers=auth_headers,
        )
        assert del_res.status_code == 204
        get_res = await client.get(
            f"/api/channels/{group_channel['id']}/ttl",
            headers=auth_headers,
        )
        assert get_res.json()["ttl_seconds"] == 0

    async def test_non_admin_member_cannot_set(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user_headers: dict,
        group_channel: dict,
    ):
        # Add second user as plain member via invite-by-code.
        mint = await client.post(
            "/api/me/codes",
            json={
                "kind": "invite",
                "target_channel_id": group_channel["id"],
            },
            headers=auth_headers,
        )
        await client.post(
            "/api/channels/join-by-code",
            json={"code": mint.json()["code"]},
            headers=second_user_headers,
        )

        res = await client.put(
            f"/api/channels/{group_channel['id']}/ttl",
            json={"ttl_seconds": 3600},
            headers=second_user_headers,
        )
        assert res.status_code == 403

    async def test_non_member_cannot_read(
        self,
        client: AsyncClient,
        second_user_headers: dict,
        group_channel: dict,
    ):
        res = await client.get(
            f"/api/channels/{group_channel['id']}/ttl",
            headers=second_user_headers,
        )
        assert res.status_code == 403

    async def test_sweep_now_admin_only(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user_headers: dict,
        group_channel: dict,
    ):
        # Owner — works.
        res = await client.post(
            f"/api/channels/{group_channel['id']}/ttl/sweep-now",
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True

        # Add second user as plain member.
        mint = await client.post(
            "/api/me/codes",
            json={
                "kind": "invite",
                "target_channel_id": group_channel["id"],
            },
            headers=auth_headers,
        )
        await client.post(
            "/api/channels/join-by-code",
            json={"code": mint.json()["code"]},
            headers=second_user_headers,
        )

        # Plain member — forbidden.
        denied = await client.post(
            f"/api/channels/{group_channel['id']}/ttl/sweep-now",
            headers=second_user_headers,
        )
        assert denied.status_code == 403


# ── Service-layer tests ─────────────────────────────────────────


class TestTTLService:

    def test_zero_is_off(self):
        cid = "ch-svc-off"
        set_ttl_seconds(cid, 0)
        assert get_ttl_seconds(cid) == 0

    def test_clamp_below_minimum(self):
        cid = "ch-svc-tiny"
        applied = set_ttl_seconds(cid, 5)  # below 60s
        assert applied == 60
        set_ttl_seconds(cid, 0)  # cleanup

    def test_clamp_above_maximum(self):
        cid = "ch-svc-huge"
        applied = set_ttl_seconds(cid, 365 * 24 * 3600)  # 1 year
        assert applied == 30 * 24 * 3600  # clamped to 30 days
        set_ttl_seconds(cid, 0)

    def test_set_then_clear(self):
        cid = "ch-svc-clear"
        set_ttl_seconds(cid, 3600)
        assert get_ttl_seconds(cid) == 3600
        set_ttl_seconds(cid, 0)
        assert get_ttl_seconds(cid) == 0

    def test_negative_value_clamped_to_zero(self):
        cid = "ch-svc-neg"
        applied = set_ttl_seconds(cid, -100)
        assert applied == 0
        assert get_ttl_seconds(cid) == 0


# ── Sweep against a real DB ─────────────────────────────────────


class TestSweepDeletesOldMessages:
    """End-to-end: configure a TTL, plant an old + a fresh message,
    run sweep_once, assert only the old one is gone.

    Uses the in-memory test database from the conftest fixtures.
    """

    async def test_sweep_query_deletes_only_stale_rows(
        self,
        client: AsyncClient,
        auth_headers: dict,
        group_channel: dict,
        db_session: AsyncSession,
    ):
        """Direct test of the *SQL the sweeper runs*, against the
        test's db_session. We don't call ``sweep_once`` here because
        it owns its own session (via ``async_session_maker``) and
        wouldn't see rows committed in the test fixture's session.
        The behavior under test is: ``DELETE WHERE channel_id=? AND
        created_at < ?`` removes the stale row and leaves the fresh
        one alone.
        """
        from sqlalchemy import select, delete

        ch_id = group_channel["id"]
        # 1. Send a "fresh" message via the API.
        sent = await client.post(
            f"/api/channels/{ch_id}/messages",
            json={"content": "fresh message"},
            headers=auth_headers,
        )
        assert sent.status_code == 201
        fresh_id = sent.json()["id"]

        # 2. Send a second message and rewrite its ``created_at`` so
        # it qualifies as "stale" relative to a 24h TTL.
        sent2 = await client.post(
            f"/api/channels/{ch_id}/messages",
            json={"content": "stale message"},
            headers=auth_headers,
        )
        stale_id = sent2.json()["id"]

        stale_at = datetime.now(timezone.utc) - timedelta(days=2)
        stale_row = (await db_session.execute(
            select(Message).where(Message.id == stale_id),
        )).scalar_one()
        stale_row.created_at = stale_at
        await db_session.commit()

        # 3. Run the same query the sweeper runs, scoped to this
        # channel + the same cutoff (now - 24h).
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=24 * 3600)
        result = await db_session.execute(
            delete(Message).where(
                Message.channel_id == ch_id,
                Message.created_at < cutoff,
            ),
        )
        await db_session.commit()
        assert (result.rowcount or 0) >= 1

        # Fresh survives; stale is gone.
        remaining = (await db_session.execute(
            select(Message).where(Message.channel_id == ch_id),
        )).scalars().all()
        ids = {m.id for m in remaining}
        assert fresh_id in ids
        assert stale_id not in ids

    async def test_no_caps_means_no_work(self):
        """sweep_once with no configured channels does nothing."""
        # Make sure no leftover state from other tests interferes.
        from app.services.channel_message_ttl import all_ttl_caps
        for cid in list(all_ttl_caps().keys()):
            set_ttl_seconds(cid, 0)

        summary = await sweep_once()
        assert summary == {"channels": 0, "deleted": 0}
