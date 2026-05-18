"""
Tests for webhook outbound integrations (task #64).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.models.channel import Channel, ChannelMember
from app.models.user import User
from app.models.webhook import WebhookDelivery
from app.services.message_service import MessageService
from app.services.webhook_service import WebhookService, _matches, _sign


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────


async def _make_user(db, username: str) -> User:
    user = User(
        username=username,
        display_name=username.capitalize(),
        password_hash="x",
        status="online",
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def _make_dm(db, a: User, b: User) -> Channel:
    ch = Channel(type="dm", name=None, created_by=a.id)
    db.add(ch)
    await db.flush()
    db.add(ChannelMember(channel_id=ch.id, user_id=a.id, role="member"))
    db.add(ChannelMember(channel_id=ch.id, user_id=b.id, role="member"))
    await db.flush()
    return ch


# ─────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────


def test_matches_wildcard_all():
    assert _matches("*", "anything.here") is True
    assert _matches("*", "x") is True


def test_matches_exact():
    assert _matches("message.created,user.created", "message.created") is True
    assert _matches("message.created", "message.deleted") is False


def test_matches_suffix_wildcard():
    assert _matches("message.*", "message.created") is True
    assert _matches("message.*", "message.deleted") is True
    assert _matches("message.*", "user.created") is False


def test_sign_is_hmac_sha256():
    body = b'{"hello":"world"}'
    secret = "topsecret"
    sig = _sign(secret, body)
    assert sig.startswith("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"


# ─────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_webhook_generates_secret(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    rec = await WebhookService.create(
        db_session,
        owner_id=user.id,
        name="Slack",
        url="https://hooks.example.com/abc",
    )
    assert rec.is_active is True
    assert len(rec.secret) >= 16
    assert rec.events == "*"


@pytest.mark.asyncio
async def test_create_webhook_validates_url(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(ValidationError):
        await WebhookService.create(
            db_session, owner_id=user.id, name="x", url="ftp://bad"
        )


@pytest.mark.asyncio
async def test_update_normalizes_events(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await WebhookService.create(
        db_session, user.id, "wh", "https://e.com/x"
    )
    updated = await WebhookService.update(
        db_session, rec.id, user.id, events=["message.created", "message.deleted"]
    )
    assert "message.created" in updated.events
    assert "message.deleted" in updated.events


@pytest.mark.asyncio
async def test_delete_webhook(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await WebhookService.create(db_session, user.id, "wh", "https://e.com/x")
    await WebhookService.delete(db_session, rec.id, user.id)
    with pytest.raises(NotFoundError):
        await WebhookService.get(db_session, rec.id, user.id)


# ─────────────────────────────────────────────────────────
# Emission
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_enqueues_to_matching(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    # 1 active that matches
    a = await WebhookService.create(
        db_session, user.id, "all", "https://e.com/a", events=["message.created"]
    )
    # 1 active that doesn't match
    b = await WebhookService.create(
        db_session, user.id, "other", "https://e.com/b", events=["user.created"]
    )
    # 1 inactive that would match
    c = await WebhookService.create(
        db_session, user.id, "off", "https://e.com/c", events=["*"]
    )
    await WebhookService.update(db_session, c.id, user.id, is_active=False)

    n = await WebhookService.emit(
        db_session, "message.created", {"k": "v"}, channel_id=None
    )
    assert n == 1


@pytest.mark.asyncio
async def test_emit_filters_by_channel(db_session):
    user = await _make_user(db_session, "alice")
    other = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, user, other)
    await db_session.commit()

    # Webhook scoped to a different channel — should not receive
    scoped = await WebhookService.create(
        db_session, user.id, "scoped", "https://e.com/x", events=["*"]
    )
    scoped.channel_id = "other-channel-id"
    await db_session.commit()

    # Unscoped webhook
    await WebhookService.create(
        db_session, user.id, "any", "https://e.com/y", events=["*"]
    )

    n = await WebhookService.emit(
        db_session, "message.created", {"x": 1}, channel_id=ch.id
    )
    assert n == 1  # only the unscoped one


# ─────────────────────────────────────────────────────────
# Delivery (mocked HTTP)
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_one_success(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    wh = await WebhookService.create(
        db_session, user.id, "wh", "https://e.com/ok", events=["*"]
    )
    await WebhookService.emit(db_session, "test.event", {"hello": "world"})

    # Find the queued delivery
    from sqlalchemy import select

    delivery = (
        await db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.webhook_id == wh.id)
        )
    ).scalar_one()

    fake_response = MagicMock(status_code=200)

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            # Verify signature header is present and valid
            assert headers["X-CommClient-Signature"].startswith("sha256=")
            assert headers["X-CommClient-Event"] == "test.event"
            return fake_response

    with patch("app.services.webhook_service.httpx.AsyncClient", _FakeClient):
        ok = await WebhookService._deliver_one(db_session, delivery)
    assert ok is True
    await db_session.refresh(delivery)
    assert delivery.status == "success"
    assert delivery.last_status_code == 200


@pytest.mark.asyncio
async def test_deliver_one_retries_on_500(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    wh = await WebhookService.create(
        db_session, user.id, "wh", "https://e.com/x", events=["*"]
    )
    await WebhookService.emit(db_session, "x", {"a": 1})

    from sqlalchemy import select

    delivery = (
        await db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.webhook_id == wh.id)
        )
    ).scalar_one()

    fake_response = MagicMock(status_code=500)

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return fake_response

    with patch("app.services.webhook_service.httpx.AsyncClient", _FakeClient):
        ok = await WebhookService._deliver_one(db_session, delivery)
    assert ok is False
    await db_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at is not None


@pytest.mark.asyncio
async def test_deliver_one_dies_after_max_attempts(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    wh = await WebhookService.create(
        db_session, user.id, "wh", "https://e.com/x", events=["*"]
    )
    await WebhookService.emit(db_session, "x", {"a": 1})

    from sqlalchemy import select

    delivery = (
        await db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.webhook_id == wh.id)
        )
    ).scalar_one()

    delivery.attempt_count = 5  # one less than _MAX_ATTEMPTS
    await db_session.commit()

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return MagicMock(status_code=502)

    with patch("app.services.webhook_service.httpx.AsyncClient", _FakeClient):
        ok = await WebhookService._deliver_one(db_session, delivery)
    assert ok is False
    await db_session.refresh(delivery)
    assert delivery.status == "dead"
    assert delivery.attempt_count == 6


# ─────────────────────────────────────────────────────────
# End-to-end: send_message → webhook enqueued
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_emits_webhook(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await WebhookService.create(
        db_session, alice.id, "wh", "https://e.com/m", events=["message.created"]
    )

    msg = await MessageService.send_message(
        db_session, ch.id, alice.id, "hello via webhook"
    )

    from sqlalchemy import select
    deliveries = (
        await db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event == "message.created")
        )
    ).scalars().all()
    assert len(deliveries) == 1
    payload = json.loads(deliveries[0].payload_json)
    assert payload["message_id"] == msg.id
    assert payload["channel_id"] == ch.id
    assert payload["content"] == "hello via webhook"
