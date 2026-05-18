"""
Tests for message templates / quick replies (task #69).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.models.channel import Channel, ChannelMember
from app.models.user import User
from app.services.template_service import TemplateService


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
# Create
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_personal_template(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    rec = await TemplateService.create(
        db_session, alice.id, shortcut="thanks", content="Thank you!"
    )
    assert rec.scope == "personal"
    assert rec.shortcut == "thanks"
    assert rec.content == "Thank you!"


@pytest.mark.asyncio
async def test_create_channel_template(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    rec = await TemplateService.create(
        db_session, alice.id, shortcut="hello", content="Hi!", channel_id=ch.id
    )
    assert rec.scope == "channel"
    assert rec.channel_id == ch.id


@pytest.mark.asyncio
async def test_channel_template_requires_membership(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    eve = await _make_user(db_session, "eve")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    with pytest.raises(ForbiddenError):
        await TemplateService.create(
            db_session, eve.id, shortcut="x", content="y", channel_id=ch.id
        )


@pytest.mark.asyncio
async def test_create_validates_shortcut(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(ValidationError):
        await TemplateService.create(db_session, alice.id, shortcut="", content="x")
    with pytest.raises(ValidationError):
        await TemplateService.create(db_session, alice.id, shortcut="has space", content="x")


@pytest.mark.asyncio
async def test_create_rejects_duplicate_in_same_scope(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    await TemplateService.create(db_session, alice.id, shortcut="dup", content="a")
    with pytest.raises(ConflictError):
        await TemplateService.create(db_session, alice.id, shortcut="dup", content="b")


# ─────────────────────────────────────────────────────────
# List / Get
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_personal(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    await TemplateService.create(db_session, alice.id, shortcut="a", content="x")
    await TemplateService.create(db_session, alice.id, shortcut="b", content="y")
    items = await TemplateService.list_for_user(db_session, alice.id)
    assert len(items) == 2


@pytest.mark.asyncio
async def test_list_includes_channel_templates_for_members(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    await TemplateService.create(
        db_session, alice.id, shortcut="ch1", content="x", channel_id=ch.id
    )
    items = await TemplateService.list_for_user(db_session, bob.id, channel_id=ch.id)
    # Bob has no personal templates but should see alice's channel template
    assert len(items) == 1
    assert items[0].scope == "channel"


@pytest.mark.asyncio
async def test_list_search_query(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    await TemplateService.create(
        db_session, alice.id, shortcut="hello", content="Hi there!"
    )
    await TemplateService.create(
        db_session, alice.id, shortcut="bye", content="See you later"
    )
    items = await TemplateService.list_for_user(db_session, alice.id, query="later")
    assert len(items) == 1
    assert items[0].shortcut == "bye"


@pytest.mark.asyncio
async def test_get_personal_only_owner(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    await db_session.commit()
    rec = await TemplateService.create(db_session, alice.id, shortcut="a", content="x")
    with pytest.raises(ForbiddenError):
        await TemplateService.get(db_session, rec.id, bob.id)


# ─────────────────────────────────────────────────────────
# Update / Delete
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_template(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await TemplateService.create(db_session, alice.id, shortcut="x", content="old")
    upd = await TemplateService.update(db_session, rec.id, alice.id, content="new")
    assert upd.content == "new"


@pytest.mark.asyncio
async def test_update_only_owner(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    await db_session.commit()
    rec = await TemplateService.create(db_session, alice.id, shortcut="x", content="o")
    with pytest.raises(ForbiddenError):
        await TemplateService.update(db_session, rec.id, bob.id, content="new")


@pytest.mark.asyncio
async def test_delete_template(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await TemplateService.create(db_session, alice.id, shortcut="x", content="o")
    await TemplateService.delete(db_session, rec.id, alice.id)
    with pytest.raises(NotFoundError):
        await TemplateService.get(db_session, rec.id, alice.id)


# ─────────────────────────────────────────────────────────
# Resolve
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_personal(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    await TemplateService.create(
        db_session, alice.id, shortcut="thanks", content="Thank you"
    )
    resolved = await TemplateService.resolve(db_session, alice.id, "thanks")
    assert resolved is not None
    assert resolved.content == "Thank you"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_missing(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()
    assert await TemplateService.resolve(db_session, alice.id, "missing") is None


@pytest.mark.asyncio
async def test_resolve_personal_overrides_channel(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    # Bob creates a channel template "hi"
    await TemplateService.create(
        db_session, bob.id, shortcut="hi", content="channel-hi", channel_id=ch.id
    )
    # Alice has a personal template with same shortcut
    await TemplateService.create(
        db_session, alice.id, shortcut="hi", content="personal-hi"
    )

    resolved = await TemplateService.resolve(db_session, alice.id, "hi", channel_id=ch.id)
    assert resolved.content == "personal-hi"


@pytest.mark.asyncio
async def test_resolve_channel_template(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()
    await TemplateService.create(
        db_session, alice.id, shortcut="rules", content="be kind", channel_id=ch.id
    )
    resolved = await TemplateService.resolve(db_session, bob.id, "rules", channel_id=ch.id)
    assert resolved is not None
    assert resolved.content == "be kind"
