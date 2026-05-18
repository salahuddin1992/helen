"""
Phase 7 / Module AH — Internal-only plugin ratings.

Local-only reviews persisted in :class:`PluginRating`. There is no
public posting endpoint; admins / operators in the same LAN can rate
plugins after install to surface quality feedback inside the
marketplace UI.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PluginRating(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "plugin_ratings"
    __table_args__ = (
        UniqueConstraint("manifest_slug", "user_id",
                         name="uq_plugin_rating_slug_user"),
        Index("ix_plugin_rating_slug", "manifest_slug"),
        Index("ix_plugin_rating_user", "user_id"),
    )

    manifest_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey("plugin_manifests.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    review: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
