"""
Tests for features added in this development cycle:
  - @mention parsing + notification dispatch (task #51)
  - Custom user status messages (task #52)
  - Channel archive/mute/pin/last-read tracking (task #53)
  - User blocking enforcement on messages and calls (task #54)
  - Scheduled messages (task #55)
  - Advanced message search filters (task #56)
  - Real waveform generation for voice messages (task #57)
  - Persistent audit log + admin query endpoint (task #50)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.channel import Channel, ChannelMember
from app.models.contact import Contact
from app.models.notification import Notification
from app.models.scheduled_message import ScheduledMessage
from app.models.user import User
from app.services.channel_service import ChannelService
from app.services.message_service import MessageService
from app.services.scheduled_message_service import ScheduledMessageService
from app.services.user_service import UserService
from app.services.voice_message_service import VoiceMessageService


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

async def _make_user(db, username: str, role: str = "user") -> User:
    user = User(
        username=username,
        display_name=username.capitalize(),
        password_hash="x",
        status="online",
        role=role,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def _make_dm(db, user_a: User, user_b: User) -> Channel:
    channel = Channel(type="dm", name=None, created_by=user_a.id)
    db.add(channel)
    await db.flush()
    db.add(ChannelMember(channel_id=channel.id, user_id=user_a.id, role="member"))
    db.add(ChannelMember(channel_id=channel.id, user_id=user_b.id, role="member"))
    await db.flush()
    return channel


async def _make_group(db, creator: User, members: list[User]) -> Channel:
    channel = Channel(type="group", name="Test Group", created_by=creator.id)
    db.add(channel)
    await db.flush()
    db.add(ChannelMember(channel_id=channel.id, user_id=creator.id, role="admin"))
    for m in members:
        db.add(ChannelMember(channel_id=channel.id, user_id=m.id, role="member"))
    await db.flush()
    return channel


# ─────────────────────────────────────────────────────────
# Task #51 — @mention parsing
# ─────────────────────────────────────────────────────────

class TestMentionExtraction:

    def test_extract_simple_mentions(self):
        out = MessageService.extract_mentions("Hi @alice and @bob_2!")
        assert out == ["alice", "bob_2"]

    def test_extract_dedupes_and_lowercases(self):
        out = MessageService.extract_mentions("@Alice @ALICE @alice")
        assert out == ["alice"]

    def test_extract_ignores_emails(self):
        out = MessageService.extract_mentions("Email noreply@example.com cc @real")
        assert out == ["real"]

    def test_extract_handles_empty(self):
        assert MessageService.extract_mentions("") == []
        assert MessageService.extract_mentions(None) == []

    def test_extract_at_start_of_string(self):
        out = MessageService.extract_mentions("@alice hello")
        assert out == ["alice"]

    def test_extract_special_tokens(self):
        out = MessageService.extract_mentions("@everyone hi @here friends")
        assert out == ["everyone", "here"]


@pytest.mark.asyncio
async def test_dispatch_mentions_creates_notifications(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    carol = await _make_user(db_session, "carol")
    channel = await _make_group(db_session, alice, [bob, carol])
    await db_session.commit()

    msg = await MessageService.send_message(
        db_session, channel.id, alice.id, "hey @bob check this"
    )
    mentioned = await MessageService.dispatch_mentions(db_session, msg, sender_username="alice")
    assert mentioned == [bob.id]

    notifs = (await db_session.execute(
        select(Notification).where(Notification.user_id == bob.id)
    )).scalars().all()
    assert len(notifs) == 1
    assert notifs[0].type == "mention"
    assert notifs[0].reference_id == msg.id


@pytest.mark.asyncio
async def test_dispatch_mentions_everyone_broadcasts(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    carol = await _make_user(db_session, "carol")
    channel = await _make_group(db_session, alice, [bob, carol])
    await db_session.commit()

    msg = await MessageService.send_message(db_session, channel.id, alice.id, "@everyone meeting time")
    mentioned = await MessageService.dispatch_mentions(db_session, msg, sender_username="alice")
    assert set(mentioned) == {bob.id, carol.id}


# ─────────────────────────────────────────────────────────
# Task #52 — Custom status messages
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_and_clear_status_message(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()

    updated = await UserService.set_status_message(db_session, user.id, "🏖️ On vacation")
    assert updated.status_message == "🏖️ On vacation"

    cleared = await UserService.set_status_message(db_session, user.id, None)
    assert cleared.status_message is None
    assert cleared.status_expires_at is None


@pytest.mark.asyncio
async def test_status_message_expiry_sweeper(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await UserService.set_status_message(db_session, user.id, "expired", status_expires_at=past)
    cleared = await UserService.expire_status_messages(db_session)
    assert cleared >= 1
    await db_session.refresh(user)
    assert user.status_message is None


@pytest.mark.asyncio
async def test_status_message_too_long_rejected(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(ValueError):
        await UserService.set_status_message(db_session, user.id, "x" * 200)


# ─────────────────────────────────────────────────────────
# Task #53 — Channel archive/mute/pin/read
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_channel_archive_toggle(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    member = await ChannelService.set_archived(db_session, channel.id, alice.id, True)
    assert member.is_archived is True
    member = await ChannelService.set_archived(db_session, channel.id, alice.id, False)
    assert member.is_archived is False


@pytest.mark.asyncio
async def test_channel_mute_with_expiry(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await ChannelService.set_muted(db_session, channel.id, alice.id, True, mute_until=past)

    cleared = await ChannelService.expire_mutes(db_session)
    assert cleared >= 1
    member = await ChannelService._get_member(db_session, channel.id, alice.id)
    assert member.is_muted is False


@pytest.mark.asyncio
async def test_channel_update_last_read(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    member = await ChannelService.update_last_read(
        db_session, channel.id, alice.id, message_id="msg-123"
    )
    assert member.last_read_message_id == "msg-123"
    assert member.last_read_at is not None


# ─────────────────────────────────────────────────────────
# Task #54 — Block enforcement
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocking_blocks_dm_send(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    db_session.add(Contact(user_id=alice.id, contact_id=bob.id, is_blocked=True))
    await db_session.commit()

    blocked, blocker = await UserService.is_blocked_either_way(db_session, alice.id, bob.id)
    assert blocked is True
    assert blocker == alice.id

    from app.core.exceptions import ForbiddenError
    with pytest.raises(ForbiddenError):
        await MessageService.send_message(db_session, channel.id, bob.id, "hi alice")
    with pytest.raises(ForbiddenError):
        await MessageService.send_message(db_session, channel.id, alice.id, "hi bob")


@pytest.mark.asyncio
async def test_blocking_does_not_block_group(db_session):
    """Group channels are not currently blocked — only DMs."""
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    carol = await _make_user(db_session, "carol")
    group = await _make_group(db_session, alice, [bob, carol])
    db_session.add(Contact(user_id=alice.id, contact_id=bob.id, is_blocked=True))
    await db_session.commit()

    msg = await MessageService.send_message(db_session, group.id, bob.id, "hello group")
    assert msg.id is not None


# ─────────────────────────────────────────────────────────
# Task #55 — Scheduled messages
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_message_validates_time(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    # Past time → reject
    with pytest.raises(ValueError):
        await ScheduledMessageService.schedule(
            db_session, alice.id, channel.id, "hi",
            send_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

    # Far future → reject
    with pytest.raises(ValueError):
        await ScheduledMessageService.schedule(
            db_session, alice.id, channel.id, "hi",
            send_at=datetime.now(timezone.utc) + timedelta(days=400),
        )

    # Valid
    s = await ScheduledMessageService.schedule(
        db_session, alice.id, channel.id, "delayed hello",
        send_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert s.status == "pending"
    assert s.content == "delayed hello"


@pytest.mark.asyncio
async def test_schedule_cancel(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    s = await ScheduledMessageService.schedule(
        db_session, alice.id, channel.id, "delayed",
        send_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    cancelled = await ScheduledMessageService.cancel(db_session, s.id, alice.id)
    assert cancelled.status == "cancelled"


# ─────────────────────────────────────────────────────────
# Task #56 — Advanced search
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_advanced_search_by_sender_and_text(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await MessageService.send_message(db_session, channel.id, alice.id, "hello world")
    await MessageService.send_message(db_session, channel.id, bob.id, "hi there")
    await MessageService.send_message(db_session, channel.id, alice.id, "another from alice")

    msgs, total = await MessageService.search_messages(
        db_session, alice.id, query_text="hello"
    )
    assert total >= 1
    assert all("hello" in m.content for m in msgs)

    msgs, total = await MessageService.search_messages(
        db_session, alice.id, sender_username="alice"
    )
    # Two messages from alice
    assert total >= 2


@pytest.mark.asyncio
async def test_advanced_search_date_range(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    channel = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    await MessageService.send_message(db_session, channel.id, alice.id, "first")
    await MessageService.send_message(db_session, channel.id, alice.id, "second")

    future = datetime.now(timezone.utc) + timedelta(days=1)
    msgs, total = await MessageService.search_messages(
        db_session, alice.id, date_from=future
    )
    assert total == 0


# ─────────────────────────────────────────────────────────
# Task #57 — Voice waveform
# ─────────────────────────────────────────────────────────

class TestVoiceWaveform:

    def test_byte_variance_fallback_length(self):
        data = bytes(range(256)) * 100
        out = VoiceMessageService._byte_variance_waveform(data, 100)
        assert len(out) == 100
        assert all(0.0 <= v <= 1.0 for v in out)

    def test_byte_variance_empty(self):
        out = VoiceMessageService._byte_variance_waveform(b"", 50)
        assert out == [0.0] * 50

    def test_pcm_to_peaks_length(self):
        # 200 samples of synthetic PCM
        import struct
        pcm = struct.pack("<200h", *([10000, -10000] * 100))
        peaks = VoiceMessageService._pcm_to_peaks(pcm, 50)
        assert len(peaks) == 50
        assert all(0.0 <= v <= 1.0 for v in peaks)

    def test_pcm_to_peaks_empty(self):
        peaks = VoiceMessageService._pcm_to_peaks(b"", 20)
        assert peaks == [0.0] * 20


# ─────────────────────────────────────────────────────────
# Task #50 — Audit log persistence
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_log_model_to_dict(db_session):
    log = AuditLog(
        event="auth.login",
        user_id="alice",
        ip_address="127.0.0.1",
        success=True,
        details_json='{"reason":"test"}',
    )
    db_session.add(log)
    await db_session.commit()
    await db_session.refresh(log)

    data = log.to_dict()
    assert data["event"] == "auth.login"
    assert data["details"] == {"reason": "test"}
    assert data["success"] is True
