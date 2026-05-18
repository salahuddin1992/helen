"""
Tests for granular channel permissions matrix (task #70).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.models.channel import Channel, ChannelMember
from app.models.user import User
from app.services.permission_service import (
    DEFAULT_ROLE_PERMS,
    PERMISSIONS,
    PermissionService,
)


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


async def _make_group(db, creator: User, members: list[tuple[User, str]]) -> Channel:
    ch = Channel(type="group", name="G", created_by=creator.id)
    db.add(ch)
    await db.flush()
    db.add(ChannelMember(channel_id=ch.id, user_id=creator.id, role="admin"))
    for u, r in members:
        db.add(ChannelMember(channel_id=ch.id, user_id=u.id, role=r))
    await db.flush()
    return ch


# ─────────────────────────────────────────────────────────
# Defaults / resolution
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_has_all_default_perms(db_session):
    alice = await _make_user(db_session, "alice")
    ch = await _make_group(db_session, alice, [])
    await db_session.commit()
    for perm in PERMISSIONS:
        assert (
            await PermissionService.has_permission(db_session, ch.id, alice.id, perm)
        ) is True


@pytest.mark.asyncio
async def test_member_has_only_basic_perms_by_default(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()

    assert await PermissionService.has_permission(db_session, ch.id, bob.id, "post")
    assert await PermissionService.has_permission(db_session, ch.id, bob.id, "react")
    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "kick")
    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "delete_any")


@pytest.mark.asyncio
async def test_non_member_has_no_perms(db_session):
    alice = await _make_user(db_session, "alice")
    eve = await _make_user(db_session, "eve")
    ch = await _make_group(db_session, alice, [])
    await db_session.commit()

    for perm in PERMISSIONS:
        assert not await PermissionService.has_permission(
            db_session, ch.id, eve.id, perm
        )


@pytest.mark.asyncio
async def test_unknown_permission_returns_false(db_session):
    alice = await _make_user(db_session, "alice")
    ch = await _make_group(db_session, alice, [])
    await db_session.commit()
    assert await PermissionService.has_permission(
        db_session, ch.id, alice.id, "totally_made_up"
    ) is False


# ─────────────────────────────────────────────────────────
# Role-level overrides
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_role_perm(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()

    # Default: members can't pin
    assert not await PermissionService.has_permission(
        db_session, ch.id, bob.id, "pin"
    )
    # Grant pin to all members
    await PermissionService.set_role_permission(
        db_session, ch.id, alice.id, "member", "pin", True
    )
    assert await PermissionService.has_permission(db_session, ch.id, bob.id, "pin")


@pytest.mark.asyncio
async def test_revoke_role_perm(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()

    # Default: members can post — revoke it
    await PermissionService.set_role_permission(
        db_session, ch.id, alice.id, "member", "post", False
    )
    assert not await PermissionService.has_permission(
        db_session, ch.id, bob.id, "post"
    )


@pytest.mark.asyncio
async def test_set_role_requires_manage_roles(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()
    # Bob is a regular member; he doesn't have manage_roles
    with pytest.raises(ForbiddenError):
        await PermissionService.set_role_permission(
            db_session, ch.id, bob.id, "member", "pin", True
        )


@pytest.mark.asyncio
async def test_set_role_validates_inputs(db_session):
    alice = await _make_user(db_session, "alice")
    ch = await _make_group(db_session, alice, [])
    await db_session.commit()
    with pytest.raises(ValidationError):
        await PermissionService.set_role_permission(
            db_session, ch.id, alice.id, "wizard", "pin", True
        )
    with pytest.raises(ValidationError):
        await PermissionService.set_role_permission(
            db_session, ch.id, alice.id, "member", "fly", True
        )


@pytest.mark.asyncio
async def test_clear_role_perm(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()
    await PermissionService.set_role_permission(
        db_session, ch.id, alice.id, "member", "pin", True
    )
    assert await PermissionService.has_permission(db_session, ch.id, bob.id, "pin")
    await PermissionService.clear_role_permission(
        db_session, ch.id, alice.id, "member", "pin"
    )
    # Falls back to default (member doesn't have pin)
    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "pin")


# ─────────────────────────────────────────────────────────
# Member-level overrides
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_override_grants_perm(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()

    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "kick")
    await PermissionService.set_member_permission(
        db_session, ch.id, alice.id, bob.id, "kick", True
    )
    assert await PermissionService.has_permission(db_session, ch.id, bob.id, "kick")


@pytest.mark.asyncio
async def test_member_override_revokes_perm(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()

    assert await PermissionService.has_permission(db_session, ch.id, bob.id, "post")
    await PermissionService.set_member_permission(
        db_session, ch.id, alice.id, bob.id, "post", False
    )
    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "post")


@pytest.mark.asyncio
async def test_member_override_takes_priority_over_role(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()
    # Role grants pin to all members
    await PermissionService.set_role_permission(
        db_session, ch.id, alice.id, "member", "pin", True
    )
    # But Bob specifically can't pin
    await PermissionService.set_member_permission(
        db_session, ch.id, alice.id, bob.id, "pin", False
    )
    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "pin")


@pytest.mark.asyncio
async def test_clear_member_override(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()
    await PermissionService.set_member_permission(
        db_session, ch.id, alice.id, bob.id, "kick", True
    )
    await PermissionService.clear_member_permission(
        db_session, ch.id, alice.id, bob.id, "kick"
    )
    assert not await PermissionService.has_permission(db_session, ch.id, bob.id, "kick")


@pytest.mark.asyncio
async def test_member_override_target_must_be_member(db_session):
    alice = await _make_user(db_session, "alice")
    eve = await _make_user(db_session, "eve")
    ch = await _make_group(db_session, alice, [])
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await PermissionService.set_member_permission(
            db_session, ch.id, alice.id, eve.id, "post", True
        )


# ─────────────────────────────────────────────────────────
# require / effective
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_raises_when_missing(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()
    with pytest.raises(ForbiddenError):
        await PermissionService.require(db_session, ch.id, bob.id, "kick")


@pytest.mark.asyncio
async def test_effective_permissions_dict(db_session):
    alice = await _make_user(db_session, "alice")
    bob = await _make_user(db_session, "bob")
    ch = await _make_group(db_session, alice, [(bob, "member")])
    await db_session.commit()
    eff = await PermissionService.effective_permissions(db_session, ch.id, bob.id)
    assert isinstance(eff, dict)
    assert eff["post"] is True
    assert eff["delete_any"] is False
    # All known perms must be in the dict
    for p in PERMISSIONS:
        assert p in eff
