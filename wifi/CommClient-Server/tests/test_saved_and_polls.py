"""
Tests for saved messages (task #62) and polls (task #63).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.channel import Channel, ChannelMember
from app.models.user import User
from app.services.message_service import MessageService
from app.services.poll_service import PollService
from app.services.saved_message_service import SavedMessageService


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


async def _make_group(db, creator: User, members: list[User]) -> Channel:
    ch = Channel(type="group", name="G", created_by=creator.id)
    db.add(ch)
    await db.flush()
    db.add(ChannelMember(channel_id=ch.id, user_id=creator.id, role="admin"))
    for m in members:
        db.add(ChannelMember(channel_id=ch.id, user_id=m.id, role="member"))
    await db.flush()
    return ch


# ─────────────────────────────────────────────────────────
# Saved messages
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_retrieve(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    msg = await MessageService.send_message(db_session, ch.id, alice.id, "remember me")

    rec = await SavedMessageService.save(
        db_session, alice.id, msg.id, folder="ideas", note="important"
    )
    assert rec.id is not None

    items, total = await SavedMessageService.list_for_user(db_session, alice.id)
    assert total == 1
    assert items[0].message_id == msg.id
    assert items[0].folder == "ideas"
    assert items[0].note == "important"


@pytest.mark.asyncio
async def test_save_is_idempotent(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    msg = await MessageService.send_message(db_session, ch.id, alice.id, "x")

    a = await SavedMessageService.save(db_session, alice.id, msg.id)
    b = await SavedMessageService.save(
        db_session, alice.id, msg.id, folder="ideas", note="updated"
    )
    assert a.id == b.id
    assert b.folder == "ideas"
    assert b.note == "updated"


@pytest.mark.asyncio
async def test_save_unknown_message_404s(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await SavedMessageService.save(db_session, alice.id, "nope-id")


@pytest.mark.asyncio
async def test_unsave(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    msg = await MessageService.send_message(db_session, ch.id, alice.id, "x")
    await SavedMessageService.save(db_session, alice.id, msg.id)
    assert await SavedMessageService.is_saved(db_session, alice.id, msg.id)
    assert await SavedMessageService.unsave(db_session, alice.id, msg.id) is True
    assert not await SavedMessageService.is_saved(db_session, alice.id, msg.id)


@pytest.mark.asyncio
async def test_list_folders(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    m1 = await MessageService.send_message(db_session, ch.id, alice.id, "1")
    m2 = await MessageService.send_message(db_session, ch.id, alice.id, "2")
    m3 = await MessageService.send_message(db_session, ch.id, alice.id, "3")
    await SavedMessageService.save(db_session, alice.id, m1.id, folder="ideas")
    await SavedMessageService.save(db_session, alice.id, m2.id, folder="ideas")
    await SavedMessageService.save(db_session, alice.id, m3.id, folder="todos")
    folders = await SavedMessageService.list_folders(db_session, alice.id)
    by_name = {f["folder"]: f["count"] for f in folders}
    assert by_name == {"ideas": 2, "todos": 1}


# ─────────────────────────────────────────────────────────
# Polls
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_poll_basic(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(
        db_session,
        creator_id=alice.id,
        channel_id=ch.id,
        question="Lunch?",
        options=["Pizza", "Sushi", "Burgers"],
    )
    assert poll.status == "open"
    assert len(poll.options) == 3
    assert {o.text for o in poll.options} == {"Pizza", "Sushi", "Burgers"}


@pytest.mark.asyncio
async def test_create_poll_validates_options(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    with pytest.raises(ValidationError):
        await PollService.create(
            db_session, alice.id, ch.id, question="x", options=["only one"]
        )
    with pytest.raises(ValidationError):
        await PollService.create(
            db_session, alice.id, ch.id, question="", options=["a", "b"]
        )


@pytest.mark.asyncio
async def test_non_member_cannot_create(db_session):
    alice = await _make_user(db_session, "alice")
    eve = await _make_user(db_session, "eve")
    ch = await _make_group(db_session, alice, [])  # eve not invited
    await db_session.commit()

    with pytest.raises(ForbiddenError):
        await PollService.create(
            db_session, eve.id, ch.id, question="?", options=["a", "b"]
        )


@pytest.mark.asyncio
async def test_single_choice_vote_replaces(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(
        db_session, alice.id, ch.id, "Q", ["A", "B", "C"], is_multi_choice=False
    )
    a_id = poll.options[0].id
    b_id = poll.options[1].id

    # Bob votes A, then changes to B — should replace
    await PollService.vote(db_session, poll.id, bob.id, [a_id])
    await PollService.vote(db_session, poll.id, bob.id, [b_id])

    res = await PollService.results(db_session, poll.id, bob.id)
    by_id = {opt["id"]: opt["votes"] for opt in res["options"]}
    assert by_id[a_id] == 0
    assert by_id[b_id] == 1
    assert res["user_voted_for"] == [b_id]


@pytest.mark.asyncio
async def test_multi_choice_allows_multiple(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(
        db_session, alice.id, ch.id, "Q", ["A", "B", "C"], is_multi_choice=True
    )
    ids = [poll.options[0].id, poll.options[2].id]
    await PollService.vote(db_session, poll.id, bob.id, ids)
    res = await PollService.results(db_session, poll.id, bob.id)
    assert sorted(res["user_voted_for"]) == sorted(ids)


@pytest.mark.asyncio
async def test_single_choice_rejects_multi(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(
        db_session, alice.id, ch.id, "Q", ["A", "B"], is_multi_choice=False
    )
    with pytest.raises(ValidationError):
        await PollService.vote(
            db_session, poll.id, bob.id, [poll.options[0].id, poll.options[1].id]
        )


@pytest.mark.asyncio
async def test_close_poll_blocks_voting(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(db_session, alice.id, ch.id, "Q", ["A", "B"])
    await PollService.close(db_session, poll.id, alice.id)
    with pytest.raises(ValidationError):
        await PollService.vote(db_session, poll.id, bob.id, [poll.options[0].id])


@pytest.mark.asyncio
async def test_close_only_creator(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(db_session, alice.id, ch.id, "Q", ["A", "B"])
    with pytest.raises(ForbiddenError):
        await PollService.close(db_session, poll.id, bob.id)


@pytest.mark.asyncio
async def test_expire_due_sweeper(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    # Create with future closes_at, then poke it into the past
    poll = await PollService.create(
        db_session,
        alice.id,
        ch.id,
        "Q",
        ["A", "B"],
        closes_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    poll.closes_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    cleared = await PollService.expire_due(db_session)
    assert cleared >= 1
    await db_session.refresh(poll)
    assert poll.status == "closed"


@pytest.mark.asyncio
async def test_retract_vote(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [bob])
    await db_session.commit()

    poll = await PollService.create(db_session, alice.id, ch.id, "Q", ["A", "B"])
    await PollService.vote(db_session, poll.id, bob.id, [poll.options[0].id])
    removed = await PollService.retract(db_session, poll.id, bob.id)
    assert removed == 1
    res = await PollService.results(db_session, poll.id, bob.id)
    assert res["total_voters"] == 0
