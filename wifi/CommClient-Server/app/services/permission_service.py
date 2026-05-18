"""
Granular per-channel permissions service.

Permission resolution order (highest priority first):
  1. Per-member override (ChannelMemberPermission row, granted=True/False)
  2. Per-channel role default (ChannelRolePermission row, granted=True/False)
  3. Hard-coded global default (DEFAULT_ROLE_PERMS)

Channels are also pre-checked for membership; non-members never have any
permissions.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.channel import Channel, ChannelMember
from app.models.channel_permission import (
    ChannelMemberPermission,
    ChannelRolePermission,
)

logger = get_logger(__name__)

# ── Permission catalogue ─────────────────────────────────────
PERMISSIONS = (
    "post",                   # send a message
    "edit_own",               # edit your own message
    "delete_own",             # delete your own message
    "delete_any",             # delete anyone's message
    "edit_any",               # edit anyone's message
    "pin",                    # pin/unpin a message
    "react",                  # add reactions
    "invite",                 # invite users to the channel
    "kick",                   # remove members from the channel
    "manage_roles",           # change member roles + per-member perms
    "manage_channel",         # rename, archive, change description
    "manage_webhooks",        # configure webhook integrations
    "mention_all",            # use @everyone / @channel
    "upload_file",            # attach files
    "create_thread",          # start threads
    "view_history",           # read prior messages (false → live-only)
    "manage_polls",           # create / close polls
)
PERMISSIONS_SET = frozenset(PERMISSIONS)

# Hard-coded defaults — what each role can do if nothing else is set.
DEFAULT_ROLE_PERMS: dict[str, frozenset[str]] = {
    "admin": frozenset(PERMISSIONS),  # everything
    "moderator": frozenset(
        {
            "post",
            "edit_own",
            "delete_own",
            "delete_any",
            "pin",
            "react",
            "invite",
            "kick",
            "mention_all",
            "upload_file",
            "create_thread",
            "view_history",
            "manage_polls",
        }
    ),
    "member": frozenset(
        {
            "post",
            "edit_own",
            "delete_own",
            "react",
            "upload_file",
            "create_thread",
            "view_history",
        }
    ),
}


def _validate_permission(name: str) -> None:
    if name not in PERMISSIONS_SET:
        raise ValidationError(f"unknown permission '{name}'")


def _validate_role(role: str) -> None:
    if role not in {"admin", "moderator", "member"}:
        raise ValidationError(f"unknown role '{role}'")


class PermissionService:
    """CRUD + check helpers for granular channel permissions."""

    # ── Resolution ────────────────────────────────────────────

    @staticmethod
    async def has_permission(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        permission: str,
    ) -> bool:
        if permission not in PERMISSIONS_SET:
            return False
        member = (
            await db.execute(
                select(ChannelMember).where(
                    and_(
                        ChannelMember.channel_id == channel_id,
                        ChannelMember.user_id == user_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if member is None:
            return False

        # 1. Per-member override
        override = (
            await db.execute(
                select(ChannelMemberPermission.granted).where(
                    and_(
                        ChannelMemberPermission.channel_id == channel_id,
                        ChannelMemberPermission.user_id == user_id,
                        ChannelMemberPermission.permission == permission,
                    )
                )
            )
        ).scalar_one_or_none()
        if override is not None:
            return bool(override)

        # 2. Per-channel role default
        role_setting = (
            await db.execute(
                select(ChannelRolePermission.granted).where(
                    and_(
                        ChannelRolePermission.channel_id == channel_id,
                        ChannelRolePermission.role == member.role,
                        ChannelRolePermission.permission == permission,
                    )
                )
            )
        ).scalar_one_or_none()
        if role_setting is not None:
            return bool(role_setting)

        # 3. Global default
        return permission in DEFAULT_ROLE_PERMS.get(member.role, frozenset())

    @staticmethod
    async def require(
        db: AsyncSession, channel_id: str, user_id: str, permission: str
    ) -> None:
        if not await PermissionService.has_permission(
            db, channel_id, user_id, permission
        ):
            raise ForbiddenError(f"Missing permission: {permission}")

    @staticmethod
    async def effective_permissions(
        db: AsyncSession, channel_id: str, user_id: str
    ) -> dict[str, bool]:
        """Return a {permission: bool} dict for every known permission."""
        return {
            p: await PermissionService.has_permission(db, channel_id, user_id, p)
            for p in PERMISSIONS
        }

    # ── Role-level grants ─────────────────────────────────────

    @staticmethod
    async def set_role_permission(
        db: AsyncSession,
        channel_id: str,
        actor_id: str,
        role: str,
        permission: str,
        granted: bool,
    ) -> ChannelRolePermission:
        _validate_role(role)
        _validate_permission(permission)
        await PermissionService.require(db, channel_id, actor_id, "manage_roles")

        ch = await db.get(Channel, channel_id)
        if ch is None:
            raise NotFoundError("Channel", channel_id)

        existing = (
            await db.execute(
                select(ChannelRolePermission).where(
                    and_(
                        ChannelRolePermission.channel_id == channel_id,
                        ChannelRolePermission.role == role,
                        ChannelRolePermission.permission == permission,
                    )
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = ChannelRolePermission(
                channel_id=channel_id,
                role=role,
                permission=permission,
                granted=granted,
            )
            db.add(existing)
        else:
            existing.granted = granted

        await db.commit()
        await db.refresh(existing)
        logger.info(
            "channel_role_permission_set",
            channel_id=channel_id,
            role=role,
            permission=permission,
            granted=granted,
        )
        return existing

    @staticmethod
    async def clear_role_permission(
        db: AsyncSession,
        channel_id: str,
        actor_id: str,
        role: str,
        permission: str,
    ) -> bool:
        _validate_role(role)
        _validate_permission(permission)
        await PermissionService.require(db, channel_id, actor_id, "manage_roles")
        result = await db.execute(
            delete(ChannelRolePermission).where(
                and_(
                    ChannelRolePermission.channel_id == channel_id,
                    ChannelRolePermission.role == role,
                    ChannelRolePermission.permission == permission,
                )
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    async def list_role_permissions(
        db: AsyncSession, channel_id: str
    ) -> list[ChannelRolePermission]:
        result = await db.execute(
            select(ChannelRolePermission)
            .where(ChannelRolePermission.channel_id == channel_id)
            .order_by(
                ChannelRolePermission.role.asc(),
                ChannelRolePermission.permission.asc(),
            )
        )
        return list(result.scalars().all())

    # ── Member-level overrides ────────────────────────────────

    @staticmethod
    async def set_member_permission(
        db: AsyncSession,
        channel_id: str,
        actor_id: str,
        target_user_id: str,
        permission: str,
        granted: bool,
    ) -> ChannelMemberPermission:
        _validate_permission(permission)
        await PermissionService.require(db, channel_id, actor_id, "manage_roles")

        # target must be a member
        is_member = (
            await db.execute(
                select(ChannelMember.user_id).where(
                    and_(
                        ChannelMember.channel_id == channel_id,
                        ChannelMember.user_id == target_user_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if is_member is None:
            raise NotFoundError("ChannelMember", target_user_id)

        existing = (
            await db.execute(
                select(ChannelMemberPermission).where(
                    and_(
                        ChannelMemberPermission.channel_id == channel_id,
                        ChannelMemberPermission.user_id == target_user_id,
                        ChannelMemberPermission.permission == permission,
                    )
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = ChannelMemberPermission(
                channel_id=channel_id,
                user_id=target_user_id,
                permission=permission,
                granted=granted,
            )
            db.add(existing)
        else:
            existing.granted = granted

        await db.commit()
        await db.refresh(existing)
        logger.info(
            "channel_member_permission_set",
            channel_id=channel_id,
            user_id=target_user_id,
            permission=permission,
            granted=granted,
        )
        return existing

    @staticmethod
    async def clear_member_permission(
        db: AsyncSession,
        channel_id: str,
        actor_id: str,
        target_user_id: str,
        permission: str,
    ) -> bool:
        _validate_permission(permission)
        await PermissionService.require(db, channel_id, actor_id, "manage_roles")
        result = await db.execute(
            delete(ChannelMemberPermission).where(
                and_(
                    ChannelMemberPermission.channel_id == channel_id,
                    ChannelMemberPermission.user_id == target_user_id,
                    ChannelMemberPermission.permission == permission,
                )
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    async def list_member_permissions(
        db: AsyncSession, channel_id: str, user_id: str
    ) -> list[ChannelMemberPermission]:
        result = await db.execute(
            select(ChannelMemberPermission)
            .where(
                and_(
                    ChannelMemberPermission.channel_id == channel_id,
                    ChannelMemberPermission.user_id == user_id,
                )
            )
            .order_by(ChannelMemberPermission.permission.asc())
        )
        return list(result.scalars().all())
