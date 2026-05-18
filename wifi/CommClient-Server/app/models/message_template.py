"""
Message templates / quick replies.

Two scopes are supported:
  - personal:  belongs to a single user (owner_id set)
  - channel:   belongs to a single channel (channel_id set, owner_id is the
               creator). Visible to all members of that channel.

Each template has a unique shortcut WITHIN ITS SCOPE — e.g. /thanks expands
to "Thanks, I appreciate it!". On the client side, typing the shortcut at the
start of a message can be substituted; the server `resolve` helper provides
the same lookup for server-side or programmatic use.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "message_templates"
    __table_args__ = (
        # Same shortcut may exist in personal AND channel scopes — uniqueness
        # is per (owner_id, channel_id, shortcut) tuple. NULL channel_id means
        # personal-scoped.
        UniqueConstraint(
            "owner_id", "channel_id", "shortcut", name="uq_template_shortcut"
        ),
        Index("ix_message_templates_owner_scope", "owner_id", "scope"),
        Index("ix_message_templates_channel", "channel_id"),
    )

    owner_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="personal"
    )  # "personal" | "channel"
    shortcut: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    owner: Mapped["User"] = relationship("User")  # noqa: F821
    channel: Mapped["Channel | None"] = relationship("Channel")  # noqa: F821

    def __repr__(self) -> str:
        return f"<MessageTemplate {self.shortcut} owner={self.owner_id[:8]}>"
