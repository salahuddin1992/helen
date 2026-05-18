"""
Tests for channel categories (task #67).
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
from app.services.channel_category_service import ChannelCategoryService


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
# Categories
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_category(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, alice.id, "Work", color="#ff0")
    assert cat.id is not None
    assert cat.name == "Work"
    assert cat.color == "#ff0"
    assert cat.sort_order == 0


@pytest.mark.asyncio
async def test_create_assigns_increasing_sort_order(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    a = await ChannelCategoryService.create(db_session, alice.id, "A")
    b = await ChannelCategoryService.create(db_session, alice.id, "B")
    c = await ChannelCategoryService.create(db_session, alice.id, "C")
    assert a.sort_order == 0
    assert b.sort_order == 1
    assert c.sort_order == 2


@pytest.mark.asyncio
async def test_create_rejects_duplicate_name(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    await ChannelCategoryService.create(db_session, alice.id, "Dup")
    with pytest.raises(ConflictError):
        await ChannelCategoryService.create(db_session, alice.id, "Dup")


@pytest.mark.asyncio
async def test_create_validates_name(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    with pytest.raises(ValidationError):
        await ChannelCategoryService.create(db_session, alice.id, "")
    with pytest.raises(ValidationError):
        await ChannelCategoryService.create(db_session, alice.id, "x" * 100)


@pytest.mark.asyncio
async def test_list_for_user_sorted(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    a = await ChannelCategoryService.create(db_session, alice.id, "A")
    b = await ChannelCategoryService.create(db_session, alice.id, "B")
    items = await ChannelCategoryService.list_for_user(db_session, alice.id)
    assert [i.id for i in items] == [a.id, b.id]


@pytest.mark.asyncio
async def test_update_category(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, alice.id, "Old")
    updated = await ChannelCategoryService.update(
        db_session, cat.id, alice.id, name="New", is_collapsed=True
    )
    assert updated.name == "New"
    assert updated.is_collapsed is True


@pytest.mark.asyncio
async def test_update_other_user_forbidden(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, alice.id, "Mine")
    with pytest.raises(ForbiddenError):
        await ChannelCategoryService.update(db_session, cat.id, bob.id, name="Hacked")


@pytest.mark.asyncio
async def test_delete_category(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, alice.id, "X")
    await ChannelCategoryService.delete(db_session, cat.id, alice.id)
    with pytest.raises(NotFoundError):
        await ChannelCategoryService.get(db_session, cat.id, alice.id)


@pytest.mark.asyncio
async def test_reorder_categories(db_session):
    alice = await _make_user(db_session, "alice")
    await db_session.commit()

    a = await ChannelCategoryService.create(db_session, alice.id, "A")
    b = await ChannelCategoryService.create(db_session, alice.id, "B")
    c = await ChannelCategoryService.create(db_session, alice.id, "C")

    items = await ChannelCategoryService.reorder(
        db_session, alice.id, [c.id, a.id, b.id]
    )
    assert [i.id for i in items] == [c.id, a.id, b.id]
    assert items[0].sort_order == 0
    assert items[2].sort_order == 2


# ─────────────────────────────────────────────────────────
# Assignments
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_channel_to_category(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, alice.id, "Friends")
    asn = await ChannelCategoryService.assign_channel(
        db_session, alice.id, cat.id, ch.id
    )
    assert asn.user_id == alice.id
    assert asn.channel_id == ch.id
    assert asn.category_id == cat.id


@pytest.mark.asyncio
async def test_assign_non_member_forbidden(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    eve = await _make_user(db_session, "eve")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, eve.id, "Eve's")
    with pytest.raises(ForbiddenError):
        await ChannelCategoryService.assign_channel(
            db_session, eve.id, cat.id, ch.id
        )


@pytest.mark.asyncio
async def test_assign_moves_between_categories(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    cat_a = await ChannelCategoryService.create(db_session, alice.id, "A")
    cat_b = await ChannelCategoryService.create(db_session, alice.id, "B")
    await ChannelCategoryService.assign_channel(db_session, alice.id, cat_a.id, ch.id)
    asn = await ChannelCategoryService.assign_channel(
        db_session, alice.id, cat_b.id, ch.id
    )
    assert asn.category_id == cat_b.id

    in_a = await ChannelCategoryService.list_assignments(
        db_session, alice.id, category_id=cat_a.id
    )
    assert in_a == []
    in_b = await ChannelCategoryService.list_assignments(
        db_session, alice.id, category_id=cat_b.id
    )
    assert len(in_b) == 1


@pytest.mark.asyncio
async def test_unassign_channel(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_dm(db_session, alice, bob)
    await db_session.commit()

    cat = await ChannelCategoryService.create(db_session, alice.id, "X")
    await ChannelCategoryService.assign_channel(db_session, alice.id, cat.id, ch.id)
    removed = await ChannelCategoryService.unassign_channel(db_session, alice.id, ch.id)
    assert removed is True
    again = await ChannelCategoryService.unassign_channel(db_session, alice.id, ch.id)
    assert again is False


@pytest.mark.asyncio
async def test_categories_isolated_per_user(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    await db_session.commit()

    await ChannelCategoryService.create(db_session, alice.id, "A")
    await ChannelCategoryService.create(db_session, bob.id, "B")
    a_items = await ChannelCategoryService.list_for_user(db_session, alice.id)
    b_items = await ChannelCategoryService.list_for_user(db_session, bob.id)
    assert len(a_items) == 1
    assert len(b_items) == 1
    assert a_items[0].name == "A"
    assert b_items[0].name == "B"
