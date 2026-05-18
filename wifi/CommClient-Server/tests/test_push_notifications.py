"""
Tests for the push notification subsystem (task #61).

Covers:
  - DeviceTokenService.register / list / deactivate
  - PushDispatcher fan-out with a fake provider
  - Auto-disabling tokens after invalid_token / repeated failures
  - NotificationService.create_notification fires push for the recipient
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.device_token import DeviceToken
from app.models.user import User
from app.services.device_token_service import DeviceTokenService
from app.services.notification_service import NotificationService
from app.services.push.dispatcher import PushDispatcher, push_dispatcher
from app.services.push.provider import PushPayload, PushResult


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


class _FakeProvider:
    """Fake PushProvider for tests — records every send_one call."""

    name = "fake"

    def __init__(
        self,
        configured: bool = True,
        result: PushResult | None = None,
    ):
        self._configured = configured
        self._result = result or PushResult(success=True, provider_message_id="msg-1")
        self.calls: list[tuple[str, PushPayload, dict | None]] = []

    async def is_configured(self) -> bool:
        return self._configured

    async def send_one(self, token, payload, *, extra=None):
        self.calls.append((token, payload, extra))
        return self._result


# ─────────────────────────────────────────────────────────
# DeviceTokenService
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_creates_token(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    rec = await DeviceTokenService.register(
        db_session,
        user_id=user.id,
        provider="fcm",
        token="abc123",
        platform="android",
        device_name="Pixel 8",
        app_version="1.0.0",
    )
    assert rec.id is not None
    assert rec.is_active is True
    assert rec.failure_count == 0


@pytest.mark.asyncio
async def test_register_upserts_on_duplicate(db_session):
    """Registering the same (provider, token) should reuse the existing row."""
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    await db_session.commit()

    a = await DeviceTokenService.register(
        db_session, user_id=alice.id, provider="fcm", token="dup", platform="android"
    )
    # bob takes ownership of the same physical device
    b = await DeviceTokenService.register(
        db_session, user_id=bob.id, provider="fcm", token="dup", platform="android"
    )
    assert a.id == b.id
    assert b.user_id == bob.id


@pytest.mark.asyncio
async def test_register_validates_provider(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        await DeviceTokenService.register(
            db_session, user_id=user.id, provider="bogus", token="x", platform="android"
        )
    with pytest.raises(ValidationError):
        await DeviceTokenService.register(
            db_session, user_id=user.id, provider="fcm", token="x", platform="bogus"
        )


@pytest.mark.asyncio
async def test_list_and_deactivate(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="t1", platform="android"
    )
    listing = await DeviceTokenService.list_for_user(db_session, user.id)
    assert any(t.id == rec.id for t in listing)

    await DeviceTokenService.deactivate(db_session, user.id, rec.id)
    await db_session.refresh(rec)
    assert rec.is_active is False


# ─────────────────────────────────────────────────────────
# PushDispatcher
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_sends_to_user_devices(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    fake = _FakeProvider()
    dispatcher = PushDispatcher()
    dispatcher.register("fcm", fake)
    dispatcher.register("apns", _FakeProvider(configured=False))

    await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="tok-A", platform="android"
    )
    await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="tok-B", platform="android"
    )

    summary = await dispatcher.dispatch(
        db_session, user.id, PushPayload(title="hi", body="msg")
    )
    assert summary["sent"] == 2
    assert summary["failed"] == 0
    assert {c[0] for c in fake.calls} == {"tok-A", "tok-B"}


@pytest.mark.asyncio
async def test_dispatcher_disables_invalid_token(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    fake = _FakeProvider(
        result=PushResult(success=False, error="not_registered", invalid_token=True)
    )
    dispatcher = PushDispatcher()
    dispatcher.register("fcm", fake)

    rec = await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="dead-token", platform="android"
    )
    summary = await dispatcher.dispatch(
        db_session, user.id, PushPayload(title="x")
    )
    assert summary["disabled"] == 1
    await db_session.refresh(rec)
    assert rec.is_active is False
    assert rec.last_error is not None


@pytest.mark.asyncio
async def test_dispatcher_disables_after_threshold(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    fake = _FakeProvider(result=PushResult(success=False, error="transient"))
    dispatcher = PushDispatcher()
    dispatcher.register("fcm", fake)

    rec = await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="flaky", platform="android"
    )
    # 5 failures should trip the threshold
    for _ in range(5):
        await dispatcher.dispatch(db_session, user.id, PushPayload(title="x"))
    await db_session.refresh(rec)
    assert rec.failure_count >= 5
    assert rec.is_active is False


@pytest.mark.asyncio
async def test_dispatcher_skips_unconfigured_provider(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    dispatcher = PushDispatcher()
    dispatcher.register("fcm", _FakeProvider(configured=False))

    rec = await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="tok", platform="android"
    )
    summary = await dispatcher.dispatch(db_session, user.id, PushPayload(title="x"))
    assert summary["skipped"] == 1
    assert summary["sent"] == 0
    await db_session.refresh(rec)
    # Token should remain active when provider just isn't configured
    assert rec.is_active is True


@pytest.mark.asyncio
async def test_notification_service_fires_push(db_session, monkeypatch):
    """Creating a notification should fan out to the user's device tokens."""
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    fake = _FakeProvider()
    # Replace providers on the singleton dispatcher used by NotificationService
    push_dispatcher.register("fcm", fake)
    push_dispatcher.register("apns", _FakeProvider(configured=False))

    await DeviceTokenService.register(
        db_session, user_id=user.id, provider="fcm", token="tok-X", platform="android"
    )

    await NotificationService.create_notification(
        db_session,
        user_id=user.id,
        type="message",
        title="New message",
        body="hello",
        reference_id="msg-1",
        reference_type="message",
    )

    assert len(fake.calls) == 1
    token, payload, _extra = fake.calls[0]
    assert token == "tok-X"
    assert payload.title == "New message"
    assert payload.data["type"] == "message"
    assert payload.data["reference_id"] == "msg-1"
