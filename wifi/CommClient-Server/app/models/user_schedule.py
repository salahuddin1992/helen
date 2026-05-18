"""
User availability models — recurring weekly working-hours rules and an optional
"away" auto-reply text. The schedule defines when a user is implicitly
"available" vs "away" — the away_message is what other users see when
attempting to message someone outside their working hours.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserScheduleRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One weekly recurring window of availability for a user.

    weekday: 0=Monday … 6=Sunday (ISO).
    start_minute / end_minute: minutes since 00:00 local time, [0, 1440].
    status: typically "online", "busy", "available". Free-form so the client
            can render whatever it wants.
    """

    __tablename__ = "user_schedule_rules"
    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_usr_weekday"),
        CheckConstraint(
            "start_minute >= 0 AND start_minute < 1440",
            name="ck_usr_start_minute",
        ),
        CheckConstraint(
            "end_minute > 0 AND end_minute <= 1440",
            name="ck_usr_end_minute",
        ),
        CheckConstraint("end_minute > start_minute", name="ck_usr_window_order"),
        Index("ix_user_schedule_rules_user_weekday", "user_id", "weekday"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    user: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<UserScheduleRule user={self.user_id[:8]} day={self.weekday} "
            f"{self.start_minute}-{self.end_minute}>"
        )


class UserAwayMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Optional auto-reply text for a user when they're outside working hours.

    Exactly one row per user (enforced by a unique constraint on user_id).
    is_active toggles whether the auto-reply applies.
    """

    __tablename__ = "user_away_messages"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_away_message"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Optional explicit override that takes priority over schedule rules.
    # If set to "always_on" the user is always available; "always_away" forces
    # the away message regardless of weekly schedule.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="schedule")

    user: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return f"<UserAwayMessage user={self.user_id[:8]} mode={self.mode}>"
