"""
Tests for message drafts (task #65).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.channel import Channel, ChannelMember
from app.models.user import User
from app.services.draft_service import DraftService
from app.services.message_service import MessageService


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
# Upsert
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_creates_new_draft(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    draft = await DraftService.upsert(
        db_session, alice.id, ch.id, "hello world (in progress)"
    )
    assert draft.id is not None
    assert draft.content == "hello world (in progress)"
    assert draft.user_id == alice.id
    assert draft.channel_id == ch.id
    assert draft.thread_root_id is None


@pytest.mark.asyncio
async def test_upsert_updates_existing(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    a = await DraftService.upsert(db_session, alice.id, ch.id, "first")
    b = await DraftService.upsert(db_session, alice.id, ch.id, "second")
    assert a.id == b.id
    assert b.content == "second"


@pytest.mark.asyncio
async def test_upsert_separate_drafts_per_thread(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    # Create a parent message for thread
    parent = await MessageService.send_message(db_session, ch.id, alice.id, "parent")

    channel_draft = await DraftService.upsert(db_session, alice.id, ch.id, "channel-level")
    thread_draft = await DraftService.upsert(
        db_session, alice.id, ch.id, "in thread", thread_root_id=parent.id
    )
    assert channel_draft.id != thread_draft.id
    assert thread_draft.thread_root_id == parent.id


@pytest.mark.asyncio
async def test_upsert_rejects_non_member(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    eve = await _make_user(db_session, "eve")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    with pytest.raises(ForbiddenError):
        await DraftService.upsert(db_session, eve.id, ch.id, "hi")


@pytest.mark.asyncio
async def test_upsert_validates_thread_parent_channel(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch1 = await _make_dm(db_session, alice, bob)
    ch2 = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    parent = await MessageService.send_message(db_session, ch1.id, alice.id, "p")

    with pytest.raises(ValidationError):
        await DraftService.upsert(
            db_session, alice.id, ch2.id, "x", thread_root_id=parent.id
        )


@pytest.mark.asyncio
async def test_upsert_validates_length(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    with pytest.raises(ValidationError):
        await DraftService.upsert(db_session, alice.id, ch.id, "x" * 17_000)


# ─────────────────────────────────────────────────────────
# Get / list / delete
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_existing(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await DraftService.upsert(db_session, alice.id, ch.id, "hello")
    rec = await DraftService.get(db_session, alice.id, ch.id)
    assert rec is not None
    assert rec.content == "hello"


@pytest.mark.asyncio
async def test_get_returns_none_when_missing(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    rec = await DraftService.get(db_session, alice.id, ch.id)
    assert rec is None


@pytest.mark.asyncio
async def test_list_for_user_excludes_other_users(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await DraftService.upsert(db_session, alice.id, ch.id, "alice's note")
    await DraftService.upsert(db_session, bob.id, ch.id, "bob's note")

    items = await DraftService.list_for_user(db_session, alice.id)
    assert len(items) == 1
    assert items[0].content == "alice's note"


@pytest.mark.asyncio
async def test_delete_by_channel(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await DraftService.upsert(db_session, alice.id, ch.id, "delete me")
    removed = await DraftService.delete(db_session, alice.id, ch.id)
    assert removed is True
    assert await DraftService.get(db_session, alice.id, ch.id) is None


@pytest.mark.asyncio
async def test_delete_by_id_only_owner(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    rec = await DraftService.upsert(db_session, alice.id, ch.id, "owner only")
    # Bob can't delete alice's draft
    removed = await DraftService.delete_by_id(db_session, bob.id, rec.id)
    assert removed is False
    # Alice can
    removed = await DraftService.delete_by_id(db_session, alice.id, rec.id)
    assert removed is True


@pytest.mark.asyncio
async def test_count_for_user(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch1 = await _make_dm(db_session, alice, bob)
    ch2 = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await DraftService.upsert(db_session, alice.id, ch1.id, "a")
    await DraftService.upsert(db_session, alice.id, ch2.id, "b")
    assert await DraftService.count_for_user(db_session, alice.id) == 2
