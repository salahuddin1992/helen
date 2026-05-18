"""
Poll service — create polls, vote, retract, close, fetch results.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.poll import Poll, PollOption, PollVote

logger = get_logger(__name__)

_MAX_OPTIONS = 12
_MAX_QUESTION_LEN = 500
_MAX_OPTION_LEN = 256


class PollService:
    """CRUD for polls and votes."""

    # ── Membership / access helpers ───────────────────────────

    @staticmethod
    async def _assert_member(db: AsyncSession, channel_id: str, user_id: str) -> None:
        result = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise ForbiddenError("Not a member of this channel")

    # ── Create / close ────────────────────────────────────────

    @staticmethod
    async def create(
        db: AsyncSession,
        creator_id: str,
        channel_id: str,
        question: str,
        options: list[str],
        is_multi_choice: bool = False,
        is_anonymous: bool = False,
        closes_at: datetime | None = None,
        message_id: str | None = None,
    ) -> Poll:
        question = (question or "").strip()
        if not question:
            raise ValidationError("question is required")
        if len(question) > _MAX_QUESTION_LEN:
            raise ValidationError(f"question must be ≤ {_MAX_QUESTION_LEN} chars")

        clean = [o.strip() for o in options if o and o.strip()]
        if len(clean) < 2:
            raise ValidationError("at least 2 options required")
        if len(clean) > _MAX_OPTIONS:
            raise ValidationError(f"max {_MAX_OPTIONS} options")
        if any(len(o) > _MAX_OPTION_LEN for o in clean):
            raise ValidationError(f"option text max {_MAX_OPTION_LEN} chars")

        if closes_at is not None:
            if closes_at.tzinfo is None:
                closes_at = closes_at.replace(tzinfo=timezone.utc)
            if closes_at <= datetime.now(timezone.utc):
                raise ValidationError("closes_at must be in the future")

        await PollService._assert_member(db, channel_id, creator_id)

        poll = Poll(
            channel_id=channel_id,
            creator_id=creator_id,
            message_id=message_id,
            question=question,
            is_multi_choice=is_multi_choice,
            is_anonymous=is_anonymous,
            closes_at=closes_at,
            status="open",
        )
        db.add(poll)
        await db.flush()

        for idx, text in enumerate(clean):
            db.add(PollOption(poll_id=poll.id, position=idx, text=text))
        await db.commit()
        await db.refresh(poll)
        # Eager-load options for the returned object
        result = await db.execute(
            select(Poll).options(selectinload(Poll.options)).where(Poll.id == poll.id)
        )
        loaded = result.scalar_one()
        logger.info(
            "poll_created",
            poll_id=loaded.id,
            channel_id=channel_id,
            options=len(clean),
            multi=is_multi_choice,
        )
        return loaded

    @staticmethod
    async def close(db: AsyncSession, poll_id: str, user_id: str) -> Poll:
        poll = await PollService._get_poll_or_404(db, poll_id)
        if poll.creator_id != user_id:
            raise ForbiddenError("Only the creator can close this poll")
        poll.status = "closed"
        await db.commit()
        # Re-fetch with options eager-loaded so callers can keep using `.options`
        return await PollService._get_poll_or_404(db, poll_id)

    @staticmethod
    async def expire_due(db: AsyncSession) -> int:
        """Sweep: close polls whose closes_at has passed. Returns rows updated."""
        from sqlalchemy import update as sql_update

        now = datetime.now(timezone.utc)
        stmt = (
            sql_update(Poll)
            .where(
                Poll.status == "open",
                Poll.closes_at.isnot(None),
                Poll.closes_at <= now,
            )
            .values(status="closed")
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount or 0

    # ── Voting ────────────────────────────────────────────────

    @staticmethod
    async def vote(
        db: AsyncSession,
        poll_id: str,
        user_id: str,
        option_ids: list[str],
    ) -> Poll:
        poll = await PollService._get_poll_or_404(db, poll_id)
        if poll.status != "open":
            raise ValidationError("Poll is closed")
        # Auto-close on access if past closes_at
        if poll.closes_at and poll.closes_at <= datetime.now(timezone.utc):
            poll.status = "closed"
            await db.commit()
            raise ValidationError("Poll is closed")

        await PollService._assert_member(db, poll.channel_id, user_id)

        if not option_ids:
            raise ValidationError("at least one option required")
        if not poll.is_multi_choice and len(option_ids) > 1:
            raise ValidationError("This poll only allows a single choice")

        # Validate options belong to this poll
        rows = (
            await db.execute(
                select(PollOption.id).where(
                    and_(
                        PollOption.poll_id == poll_id,
                        PollOption.id.in_(option_ids),
                    )
                )
            )
        ).scalars().all()
        valid_ids = set(rows)
        if len(valid_ids) != len(set(option_ids)):
            raise ValidationError("Unknown option_id for this poll")

        # Replace existing votes
        await db.execute(
            delete(PollVote).where(
                and_(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
            )
        )
        for oid in valid_ids:
            db.add(PollVote(poll_id=poll_id, option_id=oid, user_id=user_id))
        await db.commit()
        return await PollService._get_poll_or_404(db, poll_id)

    @staticmethod
    async def retract(db: AsyncSession, poll_id: str, user_id: str) -> int:
        result = await db.execute(
            delete(PollVote).where(
                and_(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
            )
        )
        await db.commit()
        return result.rowcount or 0

    # ── Read ──────────────────────────────────────────────────

    @staticmethod
    async def get(db: AsyncSession, poll_id: str, user_id: str) -> Poll:
        poll = await PollService._get_poll_or_404(db, poll_id)
        await PollService._assert_member(db, poll.channel_id, user_id)
        return poll

    @staticmethod
    async def list_for_channel(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Poll], int]:
        await PollService._assert_member(db, channel_id, user_id)
        filters = [Poll.channel_id == channel_id]
        if status:
            filters.append(Poll.status == status)
        total = (
            await db.execute(
                select(func.count()).select_from(Poll).where(and_(*filters))
            )
        ).scalar() or 0
        result = await db.execute(
            select(Poll)
            .where(and_(*filters))
            .options(selectinload(Poll.options))
            .order_by(Poll.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def results(db: AsyncSession, poll_id: str, user_id: str) -> dict:
        poll = await PollService._get_poll_or_404(db, poll_id)
        await PollService._assert_member(db, poll.channel_id, user_id)

        # Per-option vote counts
        rows = (
            await db.execute(
                select(PollOption.id, func.count(PollVote.user_id))
                .outerjoin(PollVote, PollVote.option_id == PollOption.id)
                .where(PollOption.poll_id == poll_id)
                .group_by(PollOption.id)
            )
        ).all()
        counts = {row[0]: row[1] for row in rows}
        total_voters = (
            await db.execute(
                select(func.count(func.distinct(PollVote.user_id))).where(
                    PollVote.poll_id == poll_id
                )
            )
        ).scalar() or 0

        # User's own choice
        own = (
            await db.execute(
                select(PollVote.option_id).where(
                    and_(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
                )
            )
        ).scalars().all()

        return {
            "poll_id": poll.id,
            "status": poll.status,
            "total_voters": total_voters,
            "options": [
                {"id": opt.id, "text": opt.text, "position": opt.position, "votes": counts.get(opt.id, 0)}
                for opt in poll.options
            ],
            "user_voted_for": list(own),
        }

    # ── Internals ─────────────────────────────────────────────

    @staticmethod
    async def _get_poll_or_404(db: AsyncSession, poll_id: str) -> Poll:
        result = await db.execute(
            select(Poll)
            .options(selectinload(Poll.options))
            .where(Poll.id == poll_id)
        )
        poll = result.scalar_one_or_none()
        if poll is None:
            raise NotFoundError("Poll", poll_id)
        return poll
