"""
Channel category (folder) service — per-user channel grouping.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.channel_category import ChannelCategory, ChannelCategoryAssignment

logger = get_logger(__name__)

_MAX_CATEGORIES_PER_USER = 50
_MAX_NAME_LEN = 64


class ChannelCategoryService:
    """CRUD + assignment ordering for per-user channel folders."""

    # ── Categories ────────────────────────────────────────────

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: str,
        name: str,
        color: str | None = None,
        sort_order: int | None = None,
    ) -> ChannelCategory:
        if not name or not name.strip():
            raise ValidationError("category name required")
        name = name.strip()
        if len(name) > _MAX_NAME_LEN:
            raise ValidationError(f"category name max length is {_MAX_NAME_LEN}")
        if color is not None and len(color) > 16:
            raise ValidationError("color string too long")

        existing = (
            await db.execute(
                select(func.count(ChannelCategory.id)).where(
                    ChannelCategory.user_id == user_id
                )
            )
        ).scalar() or 0
        if existing >= _MAX_CATEGORIES_PER_USER:
            raise ValidationError(
                f"max {_MAX_CATEGORIES_PER_USER} categories per user"
            )

        if sort_order is None:
            sort_order = int(existing)

        rec = ChannelCategory(
            user_id=user_id,
            name=name,
            sort_order=sort_order,
            color=color,
        )
        db.add(rec)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise ConflictError(f"category name conflict: {e}")
        await db.refresh(rec)
        logger.info("channel_category_created", user_id=user_id, name=name)
        return rec

    @staticmethod
    async def list_for_user(
        db: AsyncSession, user_id: str
    ) -> list[ChannelCategory]:
        result = await db.execute(
            select(ChannelCategory)
            .where(ChannelCategory.user_id == user_id)
            .options(selectinload(ChannelCategory.assignments))
            .order_by(ChannelCategory.sort_order.asc(), ChannelCategory.created_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get(
        db: AsyncSession, category_id: str, user_id: str
    ) -> ChannelCategory:
        rec = await db.get(ChannelCategory, category_id)
        if rec is None:
            raise NotFoundError("ChannelCategory", category_id)
        if rec.user_id != user_id:
            raise ForbiddenError("Not your category")
        return rec

    @staticmethod
    async def update(
        db: AsyncSession,
        category_id: str,
        user_id: str,
        *,
        name: str | None = None,
        sort_order: int | None = None,
        is_collapsed: bool | None = None,
        color: str | None = None,
    ) -> ChannelCategory:
        rec = await ChannelCategoryService.get(db, category_id, user_id)
        if name is not None:
            name = name.strip()
            if not name or len(name) > _MAX_NAME_LEN:
                raise ValidationError(f"name length 1..{_MAX_NAME_LEN}")
            rec.name = name
        if sort_order is not None:
            rec.sort_order = int(sort_order)
        if is_collapsed is not None:
            rec.is_collapsed = bool(is_collapsed)
        if color is not None:
            if len(color) > 16:
                raise ValidationError("color string too long")
            rec.color = color or None
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise ConflictError(f"could not update category: {e}")
        await db.refresh(rec)
        return rec

    @staticmethod
    async def delete(db: AsyncSession, category_id: str, user_id: str) -> None:
        rec = await ChannelCategoryService.get(db, category_id, user_id)
        await db.delete(rec)
        await db.commit()

    @staticmethod
    async def reorder(
        db: AsyncSession, user_id: str, ordered_ids: list[str]
    ) -> list[ChannelCategory]:
        """Bulk-set sort_order based on the position in `ordered_ids`."""
        if not ordered_ids:
            return []
        result = await db.execute(
            select(ChannelCategory).where(
                and_(
                    ChannelCategory.user_id == user_id,
                    ChannelCategory.id.in_(ordered_ids),
                )
            )
        )
        recs = {r.id: r for r in result.scalars().all()}
        if len(recs) != len(set(ordered_ids)):
            raise ValidationError("one or more category IDs are invalid")
        for idx, cid in enumerate(ordered_ids):
            recs[cid].sort_order = idx
        await db.commit()
        return await ChannelCategoryService.list_for_user(db, user_id)

    # ── Assignments ───────────────────────────────────────────

    @staticmethod
    async def assign_channel(
        db: AsyncSession,
        user_id: str,
        category_id: str,
        channel_id: str,
        sort_order: int | None = None,
    ) -> ChannelCategoryAssignment:
        # category belongs to this user
        cat = await ChannelCategoryService.get(db, category_id, user_id)
        # user must be a member of the channel
        member = (
            await db.execute(
                select(ChannelMember.user_id).where(
                    and_(
                        ChannelMember.channel_id == channel_id,
                        ChannelMember.user_id == user_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise ForbiddenError("Not a member of this channel")

        # If already assigned, move it to the new category
        existing = (
            await db.execute(
                select(ChannelCategoryAssignment).where(
                    and_(
                        ChannelCategoryAssignment.user_id == user_id,
                        ChannelCategoryAssignment.channel_id == channel_id,
                    )
                )
            )
        ).scalar_one_or_none()

        if sort_order is None:
            count = (
                await db.execute(
                    select(func.count(ChannelCategoryAssignment.user_id)).where(
                        and_(
                            ChannelCategoryAssignment.user_id == user_id,
                            ChannelCategoryAssignment.category_id == cat.id,
                        )
                    )
                )
            ).scalar() or 0
            sort_order = int(count)

        if existing is None:
            existing = ChannelCategoryAssignment(
                user_id=user_id,
                channel_id=channel_id,
                category_id=cat.id,
                sort_order=sort_order,
            )
            db.add(existing)
        else:
            existing.category_id = cat.id
            existing.sort_order = sort_order

        await db.commit()
        await db.refresh(existing)
        return existing

    @staticmethod
    async def unassign_channel(
        db: AsyncSession, user_id: str, channel_id: str
    ) -> bool:
        result = await db.execute(
            delete(ChannelCategoryAssignment).where(
                and_(
                    ChannelCategoryAssignment.user_id == user_id,
                    ChannelCategoryAssignment.channel_id == channel_id,
                )
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    async def list_assignments(
        db: AsyncSession, user_id: str, category_id: str | None = None
    ) -> list[ChannelCategoryAssignment]:
        filters = [ChannelCategoryAssignment.user_id == user_id]
        if category_id is not None:
            filters.append(ChannelCategoryAssignment.category_id == category_id)
        result = await db.execute(
            select(ChannelCategoryAssignment)
            .where(and_(*filters))
            .order_by(
                ChannelCategoryAssignment.category_id.asc(),
                ChannelCategoryAssignment.sort_order.asc(),
            )
        )
        return list(result.scalars().all())
