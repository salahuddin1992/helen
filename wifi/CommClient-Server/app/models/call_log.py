"""
Call log model — tracks call history for 1-to-1 and group calls.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CallLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "call_logs"

    channel_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    initiator_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    call_type: Mapped[str] = mapped_column(
        String(16), nullable=False,
    )  # "audio", "video", "screen_share"
    routing: Mapped[str] = mapped_column(
        String(8), nullable=False,
    )  # "p2p", "sfu"
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ringing",
    )  # "ringing", "active", "ended", "missed", "rejected"
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )  # "hangup", "timeout", "error", "rejected"
    participant_count: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    initiator: Mapped["User"] = relationship("User", foreign_keys=[initiator_id])

    def __repr__(self) -> str:
        return f"<CallLog {self.call_type} {self.status} ({self.id[:8]})>"
