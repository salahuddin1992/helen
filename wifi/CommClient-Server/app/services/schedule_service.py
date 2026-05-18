"""
User availability schedule service — manage weekly recurring availability
windows + away auto-reply text. Provides helpers to compute whether a user is
currently considered "available" or "away" based on their rules.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.user_schedule import UserAwayMessage, UserScheduleRule

logger = get_logger(__name__)

_MAX_RULES_PER_USER = 50
_MAX_AWAY_LEN = 500
_VALID_MODES = {"schedule", "always_on", "always_away"}


def _validate_window(weekday: int, start_minute: int, end_minute: int) -> None:
    if not (0 <= weekday <= 6):
        raise ValidationError("weekday must be in [0..6] (Mon..Sun)")
    if not (0 <= start_minute < 1440):
        raise ValidationError("start_minute must be in [0..1440)")
    if not (0 < end_minute <= 1440):
        raise ValidationError("end_minute must be in (0..1440]")
    if end_minute <= start_minute:
        raise ValidationError("end_minute must be greater than start_minute")


class ScheduleService:
    """CRUD + presence resolution for user availability rules."""

    # ── Rules ─────────────────────────────────────────────────

    @staticmethod
    async def add_rule(
        db: AsyncSession,
        user_id: str,
        weekday: int,
        start_minute: int,
        end_minute: int,
        status: str = "available",
        label: str | None = None,
    ) -> UserScheduleRule:
        _validate_window(weekday, start_minute, end_minute)
        if not status or len(status) > 32:
            raise ValidationError("status length 1..32")
        if label is not None and len(label) > 128:
            raise ValidationError("label too long")

        existing = (
            await db.execute(
                select(func.count(UserScheduleRule.id)).where(
                    UserScheduleRule.user_id == user_id
                )
            )
        ).scalar() or 0
        if existing >= _MAX_RULES_PER_USER:
            raise ValidationError(f"max {_MAX_RULES_PER_USER} rules per user")

        rec = UserScheduleRule(
            user_id=user_id,
            weekday=int(weekday),
            start_minute=int(start_minute),
            end_minute=int(end_minute),
            status=status,
            label=label,
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
        logger.info(
            "schedule_rule_added",
            user_id=user_id,
            weekday=weekday,
            start=start_minute,
            end=end_minute,
        )
        return rec

    @staticmethod
    async def list_rules(
        db: AsyncSession, user_id: str
    ) -> list[UserScheduleRule]:
        result = await db.execute(
            select(UserScheduleRule)
            .where(UserScheduleRule.user_id == user_id)
            .order_by(
                UserScheduleRule.weekday.asc(),
                UserScheduleRule.start_minute.asc(),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_rule(
        db: AsyncSession,
        rule_id: str,
        user_id: str,
        *,
        weekday: int | None = None,
        start_minute: int | None = None,
        end_minute: int | None = None,
        status: str | None = None,
        label: str | None = None,
    ) -> UserScheduleRule:
        rec = await db.get(UserScheduleRule, rule_id)
        if rec is None or rec.user_id != user_id:
            raise NotFoundError("UserScheduleRule", rule_id)
        new_weekday = rec.weekday if weekday is None else weekday
        new_start = rec.start_minute if start_minute is None else start_minute
        new_end = rec.end_minute if end_minute is None else end_minute
        _validate_window(new_weekday, new_start, new_end)
        rec.weekday = new_weekday
        rec.start_minute = new_start
        rec.end_minute = new_end
        if status is not None:
            if not status or len(status) > 32:
                raise ValidationError("status length 1..32")
            rec.status = status
        if label is not None:
            if len(label) > 128:
                raise ValidationError("label too long")
            rec.label = label or None
        await db.commit()
        await db.refresh(rec)
        return rec

    @staticmethod
    async def delete_rule(
        db: AsyncSession, rule_id: str, user_id: str
    ) -> bool:
        rec = await db.get(UserScheduleRule, rule_id)
        if rec is None or rec.user_id != user_id:
            return False
        await db.delete(rec)
        await db.commit()
        return True

    @staticmethod
    async def clear_rules(db: AsyncSession, user_id: str) -> int:
        result = await db.execute(
            delete(UserScheduleRule).where(UserScheduleRule.user_id == user_id)
        )
        await db.commit()
        return int(result.rowcount or 0)

    # ── Away message ──────────────────────────────────────────

    @staticmethod
    async def get_away_message(
        db: AsyncSession, user_id: str
    ) -> UserAwayMessage | None:
        result = await db.execute(
            select(UserAwayMessage).where(UserAwayMessage.user_id == user_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def set_away_message(
        db: AsyncSession,
        user_id: str,
        text: str,
        is_active: bool = True,
        mode: str = "schedule",
    ) -> UserAwayMessage:
        if not text or not text.strip():
            raise ValidationError("away message text required")
        if len(text) > _MAX_AWAY_LEN:
            raise ValidationError(f"text max length is {_MAX_AWAY_LEN} chars")
        if mode not in _VALID_MODES:
            raise ValidationError(f"mode must be one of {_VALID_MODES}")

        rec = await ScheduleService.get_away_message(db, user_id)
        if rec is None:
            rec = UserAwayMessage(
                user_id=user_id,
                text=text,
                is_active=is_active,
                mode=mode,
            )
            db.add(rec)
        else:
            rec.text = text
            rec.is_active = is_active
            rec.mode = mode
        await db.commit()
        await db.refresh(rec)
        return rec

    @staticmethod
    async def clear_away_message(db: AsyncSession, user_id: str) -> bool:
        result = await db.execute(
            delete(UserAwayMessage).where(UserAwayMessage.user_id == user_id)
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    # ── Resolution ────────────────────────────────────────────

    @staticmethod
    def _now_components(at: datetime | None = None) -> tuple[int, int]:
        """Return (weekday 0..6 Mon..Sun, minute_of_day 0..1439)."""
        moment = at or datetime.now(timezone.utc)
        return moment.weekday(), moment.hour * 60 + moment.minute

    @staticmethod
    async def is_available(
        db: AsyncSession,
        user_id: str,
        at: datetime | None = None,
    ) -> bool:
        """
        Return True if the user is currently within an availability window or
        has explicitly set always_on. False if no rules exist OR they're
        outside any window OR mode is always_away.
        """
        away = await ScheduleService.get_away_message(db, user_id)
        if away is not None and away.is_active:
            if away.mode == "always_on":
                return True
            if away.mode == "always_away":
                return False

        weekday, minute = ScheduleService._now_components(at)
        result = await db.execute(
            select(UserScheduleRule).where(
                and_(
                    UserScheduleRule.user_id == user_id,
                    UserScheduleRule.weekday == weekday,
                    UserScheduleRule.start_minute <= minute,
                    UserScheduleRule.end_minute > minute,
                )
            )
        )
        # If there ARE no rules at all, treat the user as always available
        # (no schedule = no constraints).
        rule_count = (
            await db.execute(
                select(func.count(UserScheduleRule.id)).where(
                    UserScheduleRule.user_id == user_id
                )
            )
        ).scalar() or 0
        if rule_count == 0:
            return True

        return result.scalar_one_or_none() is not None

    @staticmethod
    async def resolve_status(
        db: AsyncSession,
        user_id: str,
        at: datetime | None = None,
    ) -> dict:
        """
        Return a snapshot for the client: {available, away_text, status, mode}.
        """
        away = await ScheduleService.get_away_message(db, user_id)
        available = await ScheduleService.is_available(db, user_id, at=at)
        active_status: str | None = None

        if available:
            weekday, minute = ScheduleService._now_components(at)
            result = await db.execute(
                select(UserScheduleRule.status).where(
                    and_(
                        UserScheduleRule.user_id == user_id,
                        UserScheduleRule.weekday == weekday,
                        UserScheduleRule.start_minute <= minute,
                        UserScheduleRule.end_minute > minute,
                    )
                )
            )
            row = result.first()
            if row is not None:
                active_status = row[0]

        return {
            "available": available,
            "status": active_status,
            "away_text": (away.text if (away is not None and away.is_active and not available) else None),
            "mode": (away.mode if away is not None else "schedule"),
        }
