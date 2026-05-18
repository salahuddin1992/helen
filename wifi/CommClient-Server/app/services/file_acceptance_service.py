"""
FileAcceptance service — per-recipient file delivery / acceptance.

Owns the lifecycle of ``FileAcceptance`` rows for files shared into
channels. Public surface:

  * ``ensure_rows_for_channel_file``  — idempotent bootstrap called when
    a file message is posted; creates one pending row per channel
    member (excluding the uploader).
  * ``mark_delivered``                — called when a recipient downloads.
  * ``mark_accepted`` / ``mark_rejected``  — explicit user action.
  * ``list_for_file`` / ``list_for_recipient`` / ``summary``
  * ``pending_for_recipient``         — the "waiting for you to accept"
    inbox for a user.

Concurrency & correctness notes
-------------------------------
- All state transitions go through the model's ``mark_*`` methods so
  invariants (no bouncing out of terminal states, monotonic
  ``bytes_received``) stay consistent.
- Creating rows for a channel uses ``INSERT ... ON CONFLICT DO NOTHING``
  semantics by catching IntegrityError — safe under concurrent sends
  of the same file.
- Transition methods flush but do not commit — callers are expected to
  own the transaction boundary so they can atomically fan out socket
  events.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.file import FileRecord
from app.models.file_acceptance import (
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_PENDING,
    STATE_REJECTED,
    TERMINAL_STATES,
    VALID_STATES,
    FileAcceptance,
)

logger = get_logger(__name__)


class FileAcceptanceService:
    """Per-recipient file delivery / acceptance lifecycle."""

    # ── Row bootstrap ────────────────────────────────────────────

    @staticmethod
    async def ensure_rows_for_channel_file(
        db: AsyncSession,
        *,
        file_id: str,
        channel_id: str,
        uploader_id: str,
        message_id: str | None = None,
    ) -> list[FileAcceptance]:
        """
        Create one ``FileAcceptance`` row per channel member except the
        uploader. Idempotent under concurrent calls.

        Returns the full set of rows (newly created + pre-existing).
        """
        # Load the channel roster.
        result = await db.execute(
            select(ChannelMember.user_id).where(ChannelMember.channel_id == channel_id)
        )
        member_ids: set[str] = {row[0] for row in result.all()}
        member_ids.discard(uploader_id)

        if not member_ids:
            return []

        # Find existing rows so we only insert what's missing.
        existing_result = await db.execute(
            select(FileAcceptance).where(
                and_(
                    FileAcceptance.file_id == file_id,
                    FileAcceptance.recipient_id.in_(member_ids),
                )
            )
        )
        existing = {row.recipient_id: row for row in existing_result.scalars().all()}

        to_create = [uid for uid in member_ids if uid not in existing]
        created: list[FileAcceptance] = []
        for uid in to_create:
            row = FileAcceptance(
                file_id=file_id,
                message_id=message_id,
                recipient_id=uid,
                channel_id=channel_id,
                state=STATE_PENDING,
            )
            db.add(row)
            created.append(row)

        try:
            await db.flush()
        except IntegrityError:
            # Another concurrent caller beat us — roll back flush state
            # and re-read to absorb both sets.
            await db.rollback()
            result2 = await db.execute(
                select(FileAcceptance).where(
                    and_(
                        FileAcceptance.file_id == file_id,
                        FileAcceptance.recipient_id.in_(member_ids),
                    )
                )
            )
            return list(result2.scalars().all())

        if created:
            logger.info(
                "file_acceptance_rows_created",
                file_id=file_id,
                channel_id=channel_id,
                count=len(created),
                message_id=message_id,
            )

        # Backfill message_id on pre-existing rows if we now know it.
        if message_id:
            for row in existing.values():
                if row.message_id is None:
                    row.message_id = message_id

        return list(existing.values()) + created

    # ── State transitions ────────────────────────────────────────

    @staticmethod
    async def _get_row(
        db: AsyncSession,
        file_id: str,
        recipient_id: str,
    ) -> FileAcceptance:
        result = await db.execute(
            select(FileAcceptance).where(
                and_(
                    FileAcceptance.file_id == file_id,
                    FileAcceptance.recipient_id == recipient_id,
                )
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise NotFoundError(
                "FileAcceptance",
                f"file={file_id} recipient={recipient_id}",
            )
        return row

    @staticmethod
    async def mark_delivered(
        db: AsyncSession,
        *,
        file_id: str,
        recipient_id: str,
        bytes_received: int | None = None,
    ) -> tuple[FileAcceptance, bool]:
        """Returns (row, advanced) where advanced=True means the state changed."""
        row = await FileAcceptanceService._get_row(db, file_id, recipient_id)
        advanced = row.mark_delivered(bytes_received=bytes_received)
        await db.flush()
        return row, advanced

    @staticmethod
    async def mark_accepted(
        db: AsyncSession,
        *,
        file_id: str,
        recipient_id: str,
    ) -> tuple[FileAcceptance, bool]:
        row = await FileAcceptanceService._get_row(db, file_id, recipient_id)
        advanced = row.mark_accepted()
        if not advanced and row.state == STATE_REJECTED:
            # Caller tried to accept after rejecting — explicit error so
            # the client can prompt the user instead of silently ignoring.
            raise ValidationError(
                "cannot accept a file that was previously rejected",
            )
        await db.flush()
        return row, advanced

    @staticmethod
    async def mark_rejected(
        db: AsyncSession,
        *,
        file_id: str,
        recipient_id: str,
    ) -> tuple[FileAcceptance, bool]:
        row = await FileAcceptanceService._get_row(db, file_id, recipient_id)
        if row.state == STATE_ACCEPTED:
            raise ValidationError(
                "cannot reject a file that was already accepted",
            )
        advanced = row.mark_rejected()
        await db.flush()
        return row, advanced

    @staticmethod
    async def set_state(
        db: AsyncSession,
        *,
        file_id: str,
        recipient_id: str,
        target: str,
        bytes_received: int | None = None,
    ) -> tuple[FileAcceptance, bool]:
        """Dispatch helper for REST endpoints taking ``state`` in a body."""
        if target not in VALID_STATES:
            raise ValidationError(f"invalid state '{target}'")
        if target == STATE_DELIVERED:
            return await FileAcceptanceService.mark_delivered(
                db,
                file_id=file_id,
                recipient_id=recipient_id,
                bytes_received=bytes_received,
            )
        if target == STATE_ACCEPTED:
            return await FileAcceptanceService.mark_accepted(
                db, file_id=file_id, recipient_id=recipient_id,
            )
        if target == STATE_REJECTED:
            return await FileAcceptanceService.mark_rejected(
                db, file_id=file_id, recipient_id=recipient_id,
            )
        # target == STATE_PENDING — explicit "revert" isn't supported.
        raise ValidationError("cannot explicitly set state to 'pending'")

    # ── Queries ──────────────────────────────────────────────────

    @staticmethod
    async def list_for_file(
        db: AsyncSession,
        file_id: str,
    ) -> list[FileAcceptance]:
        result = await db.execute(
            select(FileAcceptance)
            .where(FileAcceptance.file_id == file_id)
            .order_by(FileAcceptance.created_at)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_for_recipient(
        db: AsyncSession,
        recipient_id: str,
        *,
        states: Iterable[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FileAcceptance]:
        stmt = select(FileAcceptance).where(
            FileAcceptance.recipient_id == recipient_id
        )
        if states:
            s = list(states)
            for x in s:
                if x not in VALID_STATES:
                    raise ValidationError(f"invalid state filter '{x}'")
            stmt = stmt.where(FileAcceptance.state.in_(s))
        stmt = stmt.order_by(FileAcceptance.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def pending_for_recipient(
        db: AsyncSession,
        recipient_id: str,
        *,
        limit: int = 100,
    ) -> list[FileAcceptance]:
        return await FileAcceptanceService.list_for_recipient(
            db, recipient_id, states=[STATE_PENDING, STATE_DELIVERED], limit=limit,
        )

    @staticmethod
    async def summary(
        db: AsyncSession,
        file_id: str,
    ) -> dict[str, int | str | list]:
        """
        Aggregate counts for a file: how many pending/delivered/accepted/rejected.

        Used by the uploader's client to render a "seen by / accepted by"
        badge on a shared file.
        """
        result = await db.execute(
            select(FileAcceptance.state, func.count(FileAcceptance.id))
            .where(FileAcceptance.file_id == file_id)
            .group_by(FileAcceptance.state)
        )
        counts = {state: 0 for state in VALID_STATES}
        for state, n in result.all():
            counts[state] = n

        recipients = await FileAcceptanceService.list_for_file(db, file_id)
        return {
            "file_id": file_id,
            "counts": counts,
            "total": sum(counts.values()),
            "recipients": [r.to_dict() for r in recipients],
        }
