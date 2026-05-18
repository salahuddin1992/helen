"""
Poll models — polls live inside a channel and reference an optional
"announcement" Message that introduced them.

Schema:
  Poll       — one row per poll, owned by creator, scoped to channel
  PollOption — fixed list of options for a poll (ordered by position)
  PollVote   — one row per (poll, voter, option) — supports multi-select
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Poll(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "polls"
    __table_args__ = (
        Index("ix_polls_channel_status", "channel_id", "status"),
    )

    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    creator_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    is_multi_choice: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    closes_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="open", nullable=False
    )  # "open" | "closed"

    options: Mapped[list["PollOption"]] = relationship(
        "PollOption",
        back_populates="poll",
        cascade="all, delete-orphan",
        order_by="PollOption.position",
    )
    votes: Mapped[list["PollVote"]] = relationship(
        "PollVote",
        back_populates="poll",
        cascade="all, delete-orphan",
    )
    creator: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Poll {self.id[:8]} status={self.status}>"


class PollOption(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "poll_options"
    __table_args__ = (
        UniqueConstraint("poll_id", "position", name="uq_poll_option_position"),
    )

    poll_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("polls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(256), nullable=False)

    poll: Mapped[Poll] = relationship("Poll", back_populates="options")
    votes: Mapped[list["PollVote"]] = relationship(
        "PollVote",
        back_populates="option",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<PollOption {self.id[:8]} pos={self.position}>"


class PollVote(Base, TimestampMixin):
    __tablename__ = "poll_votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", "option_id", name="uq_poll_vote"),
        Index("ix_poll_votes_user", "user_id"),
    )

    poll_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("polls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    option_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("poll_options.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    poll: Mapped[Poll] = relationship("Poll", back_populates="votes")
    option: Mapped[PollOption] = relationship("PollOption", back_populates="votes")
    user: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return f"<PollVote poll={self.poll_id[:8]} user={self.user_id[:8]}>"
