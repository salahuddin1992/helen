"""
Channel / room management service.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.models.channel import Channel, ChannelMember
from app.models.user import User

logger = get_logger(__name__)


class ChannelService:

    @staticmethod
    async def create_channel(
        db: AsyncSession,
        creator_id: str,
        channel_type: str,
        name: str | None = None,
        description: str | None = None,
        member_ids: list[str] | None = None,
    ) -> Channel:
        """Create a DM or group channel."""
        member_ids = list(member_ids or [])

        # For DM: exactly 2 members (creator + 1 other), check existing
        if channel_type == "dm":
            # Remove creator from member_ids if included (for API flexibility)
            other_ids = [mid for mid in member_ids if mid != creator_id]
            if len(other_ids) != 1:
                raise ConflictError("DM requires exactly one other member")

            other_id = other_ids[0]
            existing = await ChannelService._find_dm(db, creator_id, other_id)
            if existing:
                return existing

        channel = Channel(
            type=channel_type,
            name=name,
            description=description,
            created_by=creator_id,
        )
        db.add(channel)
        await db.flush()

        # Add creator as admin
        db.add(ChannelMember(
            channel_id=channel.id,
            user_id=creator_id,
            role="admin",
        ))

        # Add other members
        for mid in member_ids:
            if mid != creator_id:
                db.add(ChannelMember(
                    channel_id=channel.id,
                    user_id=mid,
                    role="member",
                ))

        await db.commit()
        return await ChannelService.get_channel(db, channel.id)

    @staticmethod
    async def _find_dm(db: AsyncSession, user_a: str, user_b: str) -> Channel | None:
        """Find existing DM channel between two users."""
        result = await db.execute(
            select(Channel)
            .join(ChannelMember)
            .where(
                Channel.type == "dm",
                Channel.is_active == True,
            )
            .where(
                ChannelMember.user_id.in_([user_a, user_b])
            )
            .options(
                selectinload(Channel.members).selectinload(ChannelMember.user),
            )
            .group_by(Channel.id)
            .having(func.count(ChannelMember.user_id) == 2)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_channel(db: AsyncSession, channel_id: str) -> Channel:
        result = await db.execute(
            select(Channel)
            .where(Channel.id == channel_id)
            .options(
                selectinload(Channel.members).selectinload(ChannelMember.user),
            )
        )
        channel = result.scalar_one_or_none()
        if not channel:
            raise NotFoundError("Channel", channel_id)
        return channel

    @staticmethod
    async def list_user_channels(
        db: AsyncSession,
        user_id: str,
    ) -> list[Channel]:
        result = await db.execute(
            select(Channel)
            .join(ChannelMember)
            .where(
                ChannelMember.user_id == user_id,
                Channel.is_active == True,
            )
            .options(
                selectinload(Channel.members).selectinload(ChannelMember.user),
            )
            .order_by(Channel.updated_at.desc())
        )
        return list(result.scalars().unique().all())

    @staticmethod
    async def update_channel(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        **kwargs,
    ) -> Channel:
        channel = await ChannelService.get_channel(db, channel_id)
        await ChannelService._require_admin(db, channel_id, user_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(channel, key):
                setattr(channel, key, value)
        channel.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return await ChannelService.get_channel(db, channel_id)

    @staticmethod
    async def add_member(
        db: AsyncSession,
        channel_id: str,
        requester_id: str,
        user_id: str,
        role: str = "member",
    ) -> ChannelMember:
        channel = await ChannelService.get_channel(db, channel_id)
        if channel.type == "dm":
            raise ConflictError("Cannot add members to a DM channel")

        await ChannelService._require_admin(db, channel_id, requester_id)

        # Check not already member
        existing = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError("User is already a member")

        member = ChannelMember(
            channel_id=channel_id,
            user_id=user_id,
            role=role,
        )
        db.add(member)
        await db.commit()
        logger.info("member_added", channel_id=channel_id, user_id=user_id)
        return member

    @staticmethod
    async def remove_member(
        db: AsyncSession,
        channel_id: str,
        requester_id: str,
        user_id: str,
    ) -> None:
        channel = await ChannelService.get_channel(db, channel_id)
        if channel.type == "dm":
            raise ConflictError("Cannot remove members from a DM channel")

        # Allow self-leave or admin remove
        if requester_id != user_id:
            await ChannelService._require_admin(db, channel_id, requester_id)

        result = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            raise NotFoundError("ChannelMember", user_id)
        await db.delete(member)
        await db.commit()
        logger.info("member_removed", channel_id=channel_id, user_id=user_id)

    @staticmethod
    async def is_member(db: AsyncSession, channel_id: str, user_id: str) -> bool:
        result = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def _require_admin(db: AsyncSession, channel_id: str, user_id: str) -> None:
        result = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member or member.role not in ("admin", "moderator"):
            raise ForbiddenError("You must be an admin or moderator of this channel")

    @staticmethod
    async def get_member_role(
        db: AsyncSession, channel_id: str, user_id: str
    ) -> str | None:
        """Return per-channel role string ("admin"|"moderator"|"member")
        or None if the user is not a member. Public read accessor used
        by call moderation and message moderation paths that need to
        gate actions by per-channel role rather than the global
        ``User.role``."""
        result = await db.execute(
            select(ChannelMember.role).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def is_admin_or_moderator(
        db: AsyncSession, channel_id: str, user_id: str
    ) -> bool:
        """Boolean form of get_member_role — handy for inline checks."""
        role = await ChannelService.get_member_role(db, channel_id, user_id)
        return role in ("admin", "moderator")

    # ── Per-user channel preferences (archive / mute / pin / read-state) ──

    @staticmethod
    async def _get_member(
        db: AsyncSession, channel_id: str, user_id: str
    ) -> ChannelMember:
        result = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            raise ForbiddenError("You are not a member of this channel")
        return member

    @staticmethod
    async def set_archived(
        db: AsyncSession, channel_id: str, user_id: str, archived: bool
    ) -> ChannelMember:
        member = await ChannelService._get_member(db, channel_id, user_id)
        member.is_archived = bool(archived)
        await db.commit()
        await db.refresh(member)
        logger.info(
            "channel_archive_changed",
            channel_id=channel_id, user_id=user_id, archived=archived,
        )
        return member

    @staticmethod
    async def set_pinned(
        db: AsyncSession, channel_id: str, user_id: str, pinned: bool
    ) -> ChannelMember:
        member = await ChannelService._get_member(db, channel_id, user_id)
        member.is_pinned = bool(pinned)
        await db.commit()
        await db.refresh(member)
        logger.info(
            "channel_pin_changed",
            channel_id=channel_id, user_id=user_id, pinned=pinned,
        )
        return member

    @staticmethod
    async def set_muted(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        muted: bool,
        mute_until: datetime | None = None,
    ) -> ChannelMember:
        """
        Mute/unmute a channel for a user.
        - muted=True with mute_until=None  → muted indefinitely
        - muted=True with mute_until=DT    → muted until that time
        - muted=False                      → unmute, clear mute_until
        """
        member = await ChannelService._get_member(db, channel_id, user_id)
        member.is_muted = bool(muted)
        member.mute_until = mute_until if muted else None
        await db.commit()
        await db.refresh(member)
        logger.info(
            "channel_mute_changed",
            channel_id=channel_id, user_id=user_id, muted=muted,
            until=mute_until.isoformat() if mute_until else None,
        )
        return member

    @staticmethod
    async def update_last_read(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        message_id: str | None = None,
        read_at: datetime | None = None,
    ) -> ChannelMember:
        """
        Update the user's last-read pointer for a channel.
        Both message_id and read_at are optional; missing values fall back to
        'now' for read_at and leave message_id unchanged if not provided.
        """
        member = await ChannelService._get_member(db, channel_id, user_id)
        member.last_read_at = read_at or datetime.now(timezone.utc)
        if message_id:
            member.last_read_message_id = message_id
        await db.commit()
        await db.refresh(member)
        return member

    @staticmethod
    async def expire_mutes(db: AsyncSession) -> int:
        """
        Auto-unmute channel members whose mute_until has passed.
        Returns number of rows updated. Called by a periodic background job.
        """
        from sqlalchemy import update as sql_update
        now = datetime.now(timezone.utc)
        stmt = (
            sql_update(ChannelMember)
            .where(
                ChannelMember.is_muted == True,
                ChannelMember.mute_until.isnot(None),
                ChannelMember.mute_until <= now,
            )
            .values(is_muted=False, mute_until=None)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount or 0
