"""
MessageDeadLetter model — stores messages that could not be processed or
delivered after the normal send path failed.

Why a dead-letter table?
------------------------
We already persist the message itself (always) in the ``messages`` table as
soon as it's accepted. But several downstream side effects can still fail:

  * Socket.IO fan-out crashes partway through (rare)
  * Webhook dispatch errors (external endpoints)
  * Push dispatcher errors
  * Scheduled-message deliveries that hit repeated failures
  * Cross-process message replay after a broker reconnect

Historically those failures produced a log line and silently disappeared.
The DLQ records them with enough context to:

  1. Let an admin inspect what went wrong (``/admin/dlq``).
  2. Replay the failed side-effect on demand.
  3. Drive alerting if the DLQ grows.

Fields:
  * ``status`` — ``pending`` (new), ``replaying``, ``replayed``, ``abandoned``
  * ``kind`` — the subsystem that failed: ``fanout``, ``webhook``, ``push``,
    ``scheduled``, ``notification``, ``sfu_event``, ``unknown``
  * ``reason`` — short machine-readable code
  * ``error`` — human-readable message truncated to 1KB
  * ``payload_json`` — original payload JSON for replay
  * ``attempt_count`` — how many replay attempts have been tried
  * ``last_attempt_at`` — timestamp of last replay attempt
  * ``next_attempt_at`` — scheduled retry (exponential backoff)
  * ``resolved_at`` — when the row reaches a terminal state
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageDeadLetter(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "message_dead_letters"
    __table_args__ = (
        Index("idx_dlq_status_next_attempt", "status", "next_attempt_at"),
        Index("idx_dlq_kind_status", "kind", "status"),
        Index("idx_dlq_channel_id", "channel_id"),
    )

    # Reference to the original message if any. May be NULL when a send
    # failed before the row was persisted (rare but possible).
    message_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    channel_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sender_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Machine-readable subsystem label.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Short reason code.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    # Full error text. Truncated to 1KB by the service.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-serialized original payload (message data, webhook body, etc.).
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | replaying | replayed | abandoned
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Optional operator note (set via admin REST).
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<MessageDeadLetter id={self.id[:8]} kind={self.kind} "
            f"status={self.status} attempts={self.attempt_count}>"
        )
