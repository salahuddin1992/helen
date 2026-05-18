"""
Tests for message edit history (task #66).
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.channel import Channel, ChannelMember
from app.models.user import User
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


@pytest.mark.asyncio
async def test_edit_records_history_entry(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    msg = await MessageService.send_message(db_session, ch.id, alice.id, "v1")
    await MessageService.edit_message(db_session, msg.id, alice.id, "v2")

    history = await MessageService.get_edit_history(db_session, msg.id, alice.id)
    assert len(history) == 1
    assert history[0].previous_content == "v1"
    assert history[0].editor_id == alice.id


@pytest.mark.asyncio
async def test_multiple_edits_appended_chronologically(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    msg = await MessageService.send_message(db_session, ch.id, alice.id, "v1")
    await MessageService.edit_message(db_session, msg.id, alice.id, "v2")
    await asyncio.sleep(0.01)
    await MessageService.edit_message(db_session, msg.id, alice.id, "v3")
    await asyncio.sleep(0.01)
    await MessageService.edit_message(db_session, msg.id, alice.id, "v4")

    history = await MessageService.get_edit_history(db_session, msg.id, alice.id)
    assert len(history) == 3
    assert history[0].previous_content == "v1"
    assert history[1].previous_content == "v2"
    assert history[2].previous_content == "v3"
    # Each row's edited_at should be monotonic
    assert history[0].edited_at <= history[1].edited_at <= history[2].edited_at


@pytest.mark.asyncio
async def test_no_op_edit_does_not_record_history(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    msg = await MessageService.send_message(db_session, ch.id, alice.id, "same")
    await MessageService.edit_message(db_session, msg.id, alice.id, "same")
    history = await MessageService.get_edit_history(db_session, msg.id, alice.id)
    assert history == []


@pytest.mark.asyncio
async def test_history_visible_to_channel_members(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    msg = await MessageService.send_message(db_session, ch.id, alice.id, "first")
    await MessageService.edit_message(db_session, msg.id, alice.id, "edited")

    # Bob is a member, should be able to read history
    history = await MessageService.get_edit_history(db_session, msg.id, bob.id)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_history_blocked_for_non_member(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    eve = await _make_user(db_session, "eve")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    msg = await MessageService.send_message(db_session, ch.id, alice.id, "first")
    await MessageService.edit_message(db_session, msg.id, alice.id, "edited")

    with pytest.raises(ForbiddenError):
        await MessageService.get_edit_history(db_session, msg.id, eve.id)


@pytest.mark.asyncio
async def test_history_for_unknown_message(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await MessageService.get_edit_history(db_session, "missing-id", alice.id)
