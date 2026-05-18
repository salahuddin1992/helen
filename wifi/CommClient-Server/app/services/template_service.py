"""
Message template (quick reply) service.
"""

from __future__ import annotations

import re

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.message_template import MessageTemplate

logger = get_logger(__name__)

_MAX_TEMPLATES_PER_USER = 200
_MAX_SHORTCUT_LEN = 64
_MAX_TITLE_LEN = 128
_MAX_CONTENT_LEN = 4_000

_SHORTCUT_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")


def _validate_shortcut(shortcut: str) -> None:
    if not shortcut or not shortcut.strip():
        raise ValidationError("shortcut required")
    if not _SHORTCUT_RE.match(shortcut):
        raise ValidationError(
            "shortcut may contain only letters, digits, _ . / : -"
        )


class TemplateService:
    """CRUD + resolution for message templates."""

    @staticmethod
    async def _assert_channel_member(
        db: AsyncSession, channel_id: str, user_id: str
    ) -> None:
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
    async def create(
        db: AsyncSession,
        owner_id: str,
        shortcut: str,
        content: str,
        title: str | None = None,
        channel_id: str | None = None,
    ) -> MessageTemplate:
        _validate_shortcut(shortcut)
        if not content or not content.strip():
            raise ValidationError("content required")
        if len(content) > _MAX_CONTENT_LEN:
            raise ValidationError(f"content max length is {_MAX_CONTENT_LEN}")
        if title is not None and len(title) > _MAX_TITLE_LEN:
            raise ValidationError(f"title max length is {_MAX_TITLE_LEN}")

        scope = "personal" if channel_id is None else "channel"
        if channel_id is not None:
            await TemplateService._assert_channel_member(db, channel_id, owner_id)

        # Per-user template count cap
        existing = (
            await db.execute(
                select(func.count(MessageTemplate.id)).where(
                    MessageTemplate.owner_id == owner_id
                )
            )
        ).scalar() or 0
        if existing >= _MAX_TEMPLATES_PER_USER:
            raise ValidationError(
                f"max {_MAX_TEMPLATES_PER_USER} templates per user"
            )

        # SQLite (and most engines) treat NULL as distinct in UNIQUE
        # constraints, so we cannot rely on the table-level UniqueConstraint
        # for personal-scoped templates (channel_id IS NULL). Pre-check.
        dup_clause = and_(
            MessageTemplate.owner_id == owner_id,
            MessageTemplate.shortcut == shortcut,
            MessageTemplate.channel_id.is_(channel_id)
            if channel_id is None
            else MessageTemplate.channel_id == channel_id,
        )
        dup = (
            await db.execute(select(MessageTemplate.id).where(dup_clause))
        ).scalar_one_or_none()
        if dup is not None:
            raise ConflictError(
                f"shortcut '{shortcut}' already exists in this scope"
            )

        rec = MessageTemplate(
            owner_id=owner_id,
            channel_id=channel_id,
            scope=scope,
            shortcut=shortcut,
            title=title,
            content=content,
        )
        db.add(rec)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise ConflictError(f"shortcut conflict: {e}")
        await db.refresh(rec)
        logger.info(
            "template_created", owner_id=owner_id, shortcut=shortcut, scope=scope
        )
        return rec

    @staticmethod
    async def get(
        db: AsyncSession, template_id: str, user_id: str
    ) -> MessageTemplate:
        rec = await db.get(MessageTemplate, template_id)
        if rec is None:
            raise NotFoundError("MessageTemplate", template_id)
        # Personal: only owner can see
        if rec.scope == "personal" and rec.owner_id != user_id:
            raise ForbiddenError("Not your template")
        # Channel-scoped: any member of the channel can read
        if rec.scope == "channel" and rec.channel_id is not None:
            try:
                await TemplateService._assert_channel_member(db, rec.channel_id, user_id)
            except ForbiddenError:
                raise ForbiddenError("Not a member of the template's channel")
        return rec

    @staticmethod
    async def update(
        db: AsyncSession,
        template_id: str,
        owner_id: str,
        *,
        shortcut: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> MessageTemplate:
        rec = await db.get(MessageTemplate, template_id)
        if rec is None:
            raise NotFoundError("MessageTemplate", template_id)
        if rec.owner_id != owner_id:
            raise ForbiddenError("Only the owner can update this template")

        if shortcut is not None:
            _validate_shortcut(shortcut)
            rec.shortcut = shortcut
        if title is not None:
            if len(title) > _MAX_TITLE_LEN:
                raise ValidationError(f"title max length is {_MAX_TITLE_LEN}")
            rec.title = title or None
        if content is not None:
            if not content.strip():
                raise ValidationError("content required")
            if len(content) > _MAX_CONTENT_LEN:
                raise ValidationError(f"content max length is {_MAX_CONTENT_LEN}")
            rec.content = content

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise ConflictError(f"shortcut conflict: {e}")
        await db.refresh(rec)
        return rec

    @staticmethod
    async def delete(
        db: AsyncSession, template_id: str, owner_id: str
    ) -> None:
        rec = await db.get(MessageTemplate, template_id)
        if rec is None:
            raise NotFoundError("MessageTemplate", template_id)
        if rec.owner_id != owner_id:
            raise ForbiddenError("Only the owner can delete this template")
        await db.delete(rec)
        await db.commit()

    @staticmethod
    async def list_for_user(
        db: AsyncSession,
        user_id: str,
        channel_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> list[MessageTemplate]:
        """
        Return templates visible to the user:
          - All personal templates owned by the user
          - All channel templates for `channel_id` (if user is a member)
        Optional substring search across shortcut + title + content.
        """
        clauses = [
            and_(
                MessageTemplate.owner_id == user_id,
                MessageTemplate.scope == "personal",
            )
        ]
        if channel_id is not None:
            await TemplateService._assert_channel_member(db, channel_id, user_id)
            clauses.append(
                and_(
                    MessageTemplate.scope == "channel",
                    MessageTemplate.channel_id == channel_id,
                )
            )
        stmt = select(MessageTemplate).where(or_(*clauses))

        if query:
            q = f"%{query}%"
            stmt = stmt.where(
                or_(
                    MessageTemplate.shortcut.like(q),
                    MessageTemplate.title.like(q),
                    MessageTemplate.content.like(q),
                )
            )
        stmt = stmt.order_by(MessageTemplate.shortcut.asc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def resolve(
        db: AsyncSession,
        user_id: str,
        shortcut: str,
        channel_id: str | None = None,
    ) -> MessageTemplate | None:
        """
        Find the template that should fire for `shortcut` in this context.
        Personal templates take priority over channel templates with the same
        shortcut. Returns None if no template matches.
        """
        # Personal first
        personal = (
            await db.execute(
                select(MessageTemplate).where(
                    and_(
                        MessageTemplate.owner_id == user_id,
                        MessageTemplate.scope == "personal",
                        MessageTemplate.shortcut == shortcut,
                    )
                )
            )
        ).scalar_one_or_none()
        if personal is not None:
            return personal
        if channel_id is None:
            return None
        # Channel scoped — caller must be a member
        await TemplateService._assert_channel_member(db, channel_id, user_id)
        result = await db.execute(
            select(MessageTemplate).where(
                and_(
                    MessageTemplate.scope == "channel",
                    MessageTemplate.channel_id == channel_id,
                    MessageTemplate.shortcut == shortcut,
                )
            )
        )
        return result.scalar_one_or_none()
