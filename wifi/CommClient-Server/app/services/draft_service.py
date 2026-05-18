"""
Message draft service — per-user, per-channel (and optionally per-thread)
unsent message storage. Drafts are private to the owner.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.message import Message
from app.models.message_draft import MessageDraft

logger = get_logger(__name__)

_MAX_DRAFT_LEN = 16_000
_MAX_EXTRA_LEN = 8_000


class DraftService:
    """CRUD for per-user message drafts."""

    @staticmethod
    async def _assert_member(db: AsyncSession, channel_id: str, user_id: str) -> None:
        result = await db.execute(
            select(ChannelMember.user_id).where(
                and_(
                    ChannelMember.channel_id == channel_id,
                    ChannelMember.user_id == user_id,
                )
            )
        )
        if result.scalar_one_or_none() is None:
            raise ForbiddenError("Not a member of this channel")

    @staticmethod
    async def upsert(
        db: AsyncSession,
        user_id: str,
        channel_id: str,
        content: str,
        thread_root_id: str | None = None,
        extra_json: str | None = None,
    ) -> MessageDraft:
        if content is None:
            content = ""
        if len(content) > _MAX_DRAFT_LEN:
            raise ValidationError(f"draft content max length is {_MAX_DRAFT_LEN} chars")
        if extra_json is not None and len(extra_json) > _MAX_EXTRA_LEN:
            raise ValidationError(f"extra_json max length is {_MAX_EXTRA_LEN} chars")

        await DraftService._assert_member(db, channel_id, user_id)

        if thread_root_id is not None:
            parent = await db.get(Message, thread_root_id)
            if parent is None or parent.deleted_at is not None:
                raise NotFoundError("Message", thread_root_id)
            if parent.channel_id != channel_id:
                raise ValidationError("thread_root_id does not belong to channel")

        result = await db.execute(
            select(MessageDraft).where(
                and_(
                    MessageDraft.user_id == user_id,
                    MessageDraft.channel_id == channel_id,
                    MessageDraft.thread_root_id.is_(thread_root_id)
                    if thread_root_id is None
                    else MessageDraft.thread_root_id == thread_root_id,
                )
            )
        )
        rec = result.scalar_one_or_none()

        if rec is None:
            rec = MessageDraft(
                user_id=user_id,
                channel_id=channel_id,
                thread_root_id=thread_root_id,
                content=content,
                extra_json=extra_json,
            )
            db.add(rec)
        else:
            rec.content = content
            if extra_json is not None:
                rec.extra_json = extra_json

        await db.commit()
        await db.refresh(rec)
        return rec

    @staticmethod
    async def get(
        db: AsyncSession,
        user_id: str,
        channel_id: str,
        thread_root_id: str | None = None,
    ) -> MessageDraft | None:
        result = await db.execute(
            select(MessageDraft).where(
                and_(
                    MessageDraft.user_id == user_id,
                    MessageDraft.channel_id == channel_id,
                    MessageDraft.thread_root_id.is_(thread_root_id)
                    if thread_root_id is None
                    else MessageDraft.thread_root_id == thread_root_id,
                )
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: str) -> list[MessageDraft]:
        result = await db.execute(
            select(MessageDraft)
            .where(MessageDraft.user_id == user_id)
            .order_by(MessageDraft.updated_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete(
        db: AsyncSession,
        user_id: str,
        channel_id: str,
        thread_root_id: str | None = None,
    ) -> bool:
        stmt = delete(MessageDraft).where(
            and_(
                MessageDraft.user_id == user_id,
                MessageDraft.channel_id == channel_id,
                MessageDraft.thread_root_id.is_(thread_root_id)
                if thread_root_id is None
                else MessageDraft.thread_root_id == thread_root_id,
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    async def delete_by_id(db: AsyncSession, user_id: str, draft_id: str) -> bool:
        rec = await db.get(MessageDraft, draft_id)
        if rec is None or rec.user_id != user_id:
            return False
        await db.delete(rec)
        await db.commit()
        return True

    @staticmethod
    async def count_for_user(db: AsyncSession, user_id: str) -> int:
        result = await db.execute(
            select(func.count(MessageDraft.id)).where(MessageDraft.user_id == user_id)
        )
        return int(result.scalar() or 0)
