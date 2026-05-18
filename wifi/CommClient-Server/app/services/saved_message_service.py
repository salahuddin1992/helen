"""
Saved-message (bookmarks) service.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.message import Message
from app.models.saved_message import SavedMessage

logger = get_logger(__name__)


class SavedMessageService:
    """CRUD for per-user message bookmarks."""

    @staticmethod
    async def save(
        db: AsyncSession,
        user_id: str,
        message_id: str,
        folder: str | None = None,
        note: str | None = None,
    ) -> SavedMessage:
        # Validate the message exists (and is not soft-deleted)
        msg = await db.get(Message, message_id)
        if msg is None or msg.deleted_at is not None:
            raise NotFoundError("Message", message_id)

        if folder and len(folder) > 64:
            raise ValidationError("folder name max length is 64 chars")
        if note and len(note) > 1024:
            raise ValidationError("note max length is 1024 chars")

        # Reuse existing if any (return idempotent)
        existing = await db.execute(
            select(SavedMessage).where(
                and_(
                    SavedMessage.user_id == user_id,
                    SavedMessage.message_id == message_id,
                )
            )
        )
        record = existing.scalar_one_or_none()
        if record is not None:
            # Update folder/note if provided
            updated = False
            if folder is not None and record.folder != folder:
                record.folder = folder
                updated = True
            if note is not None and record.note != note:
                record.note = note
                updated = True
            if updated:
                await db.commit()
                await db.refresh(record)
            return record

        record = SavedMessage(
            user_id=user_id,
            message_id=message_id,
            folder=folder,
            note=note,
        )
        db.add(record)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise ConflictError(f"Could not save bookmark: {e}")
        await db.refresh(record)
        logger.info("saved_message_added", user_id=user_id, message_id=message_id, folder=folder)
        return record

    @staticmethod
    async def unsave(db: AsyncSession, user_id: str, message_id: str) -> bool:
        result = await db.execute(
            delete(SavedMessage).where(
                and_(
                    SavedMessage.user_id == user_id,
                    SavedMessage.message_id == message_id,
                )
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    async def update_note(
        db: AsyncSession,
        user_id: str,
        message_id: str,
        folder: str | None = None,
        note: str | None = None,
    ) -> SavedMessage:
        result = await db.execute(
            select(SavedMessage).where(
                and_(
                    SavedMessage.user_id == user_id,
                    SavedMessage.message_id == message_id,
                )
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError("SavedMessage", message_id)
        if folder is not None:
            if len(folder) > 64:
                raise ValidationError("folder name max length is 64 chars")
            record.folder = folder or None
        if note is not None:
            if len(note) > 1024:
                raise ValidationError("note max length is 1024 chars")
            record.note = note or None
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def list_for_user(
        db: AsyncSession,
        user_id: str,
        folder: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SavedMessage], int]:
        filters = [SavedMessage.user_id == user_id]
        if folder is not None:
            filters.append(SavedMessage.folder == folder)

        total = (
            await db.execute(
                select(func.count()).select_from(SavedMessage).where(and_(*filters))
            )
        ).scalar() or 0

        result = await db.execute(
            select(SavedMessage)
            .where(and_(*filters))
            .options(selectinload(SavedMessage.message).selectinload(Message.sender))
            .order_by(SavedMessage.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def list_folders(db: AsyncSession, user_id: str) -> list[dict]:
        """Return distinct folders + counts for the user."""
        stmt = (
            select(SavedMessage.folder, func.count())
            .where(SavedMessage.user_id == user_id)
            .group_by(SavedMessage.folder)
            .order_by(SavedMessage.folder.asc())
        )
        result = await db.execute(stmt)
        return [{"folder": row[0], "count": row[1]} for row in result.all()]

    @staticmethod
    async def is_saved(db: AsyncSession, user_id: str, message_id: str) -> bool:
        result = await db.execute(
            select(SavedMessage.id).where(
                and_(
                    SavedMessage.user_id == user_id,
                    SavedMessage.message_id == message_id,
                )
            )
        )
        return result.scalar_one_or_none() is not None
