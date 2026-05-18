"""
Active call persistence models.

Why these exist
---------------
The original ``CallService`` keeps all active call state in Python dicts.
That dies on process restart, can't be shared across uvicorn workers, and
leaves orphans if the app crashes mid-call.

These tables are the **source of truth** for live calls. The in-memory cache
inside ``CallService`` stays as a hot path; mutations replicate to the DB via
``CallStatePersistence`` so that:

  * A worker restart rehydrates active calls from disk
  * multi-worker uvicorn can coordinate via row-level locking
  * Orphan cleanup and analytics read the same data
  * The frontend can reconnect mid-call and get accurate state

Lifecycle
---------
  ringing → active → ended (rows retained for audit + quick history),
  but a nightly GC task moves ``ended`` rows into ``call_logs`` (existing
  history table) so ``active_calls`` stays small.
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

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class ActiveCall(Base, TimestampMixin):
    """Live call row — persisted until status='ended' + TTL."""

    __tablename__ = "active_calls"

    # Use the call_id as primary key (not a new UUID) so in-memory and DB agree.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    channel_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    initiator_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    call_type: Mapped[str] = mapped_column(String(16), nullable=False)        # audio|video
    routing: Mapped[str] = mapped_column(String(8), nullable=False, default="mesh")  # p2p|mesh|sfu|hybrid
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ringing", index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True,
    )

    max_participants: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Generation counter — increments on every topology/routing switch; used by
    # clients to discard stale SDP from the previous topology.
    topology_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Opaque JSON — SFU router id, mediasoup worker pid, quality metrics, etc.
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Origin server in a federated cluster — the Helen server that
    # holds the authoritative ActiveCall in-memory state. Sibling
    # servers see the same DB row but only the origin can mutate
    # state (accept/reject/leave/hangup go through federation RPC).
    # NULL on legacy rows means "this server" (best-effort).
    origin_server_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )

    # Relationships
    participants: Mapped[list["ActiveCallParticipant"]] = relationship(
        "ActiveCallParticipant",
        back_populates="call",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    signals: Mapped[list["CallSignalEvent"]] = relationship(
        "CallSignalEvent",
        back_populates="call",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_active_calls_status_heartbeat", "status", "last_heartbeat_at"),
        Index("ix_active_calls_channel_status", "channel_id", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ActiveCall {self.id[:8]} {self.call_type}/{self.routing} {self.status}>"


class ActiveCallParticipant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A user's membership in a live call."""

    __tablename__ = "active_call_participants"

    call_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("active_calls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="participant")  # initiator|participant|sfu-producer

    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    video_off: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sharing_screen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Last quality snapshot for quick reads
    last_quality_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_quality_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped[ActiveCall] = relationship("ActiveCall", back_populates="participants")

    __table_args__ = (
        UniqueConstraint("call_id", "user_id", name="uq_active_call_participant"),
        Index("ix_active_participant_user_live", "user_id", "left_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ActiveCallParticipant {self.user_id[:8]} in {self.call_id[:8]}>"


class CallSignalEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Persistent signaling audit log — every SDP/ICE exchange is appended here so
    a reconnecting client can request ``signal_replay`` and rebuild the ICE
    pairing without restarting the call.
    """

    __tablename__ = "call_signal_events"

    call_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("active_calls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_user: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    to_user: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # offer|answer|ice|renegotiate|topology
    payload: Mapped[str] = mapped_column(Text, nullable=False)     # JSON blob
    topology_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    call: Mapped[ActiveCall] = relationship("ActiveCall", back_populates="signals")

    __table_args__ = (
        Index("ix_call_signal_call_kind", "call_id", "kind"),
    )
