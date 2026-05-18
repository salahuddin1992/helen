"""
Per-user channel categories — like Slack/Discord folders. Each user has their
own collapsible groupings; channels are placed in a category via
ChannelCategoryAssignment, which is also per-user (so the same channel can be
filed differently by different users).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelCategory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "channel_categories"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_channel_category_name"),
        Index("ix_channel_categories_user_sort", "user_id", "sort_order"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_collapsed: Mapped[bool] = mapped_column(default=False, nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    user: Mapped["User"] = relationship("User")  # noqa: F821
    assignments: Mapped[list["ChannelCategoryAssignment"]] = relationship(
        "ChannelCategoryAssignment",
        back_populates="category",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ChannelCategory {self.name} user={self.user_id[:8]}>"


class ChannelCategoryAssignment(Base, TimestampMixin):
    """A single channel placed inside a category for a single user."""

    __tablename__ = "channel_category_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "channel_id", name="uq_channel_category_assignment"
        ),
        Index("ix_cca_user_category_sort", "user_id", "category_id", "sort_order"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channel_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    category: Mapped["ChannelCategory"] = relationship(
        "ChannelCategory", back_populates="assignments"
    )
    channel: Mapped["Channel"] = relationship("Channel")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<ChannelCategoryAssignment user={self.user_id[:8]} "
            f"ch={self.channel_id[:8]} cat={self.category_id[:8]}>"
        )
