"""
Route trace persistence — observability layer for the broker fabric.

Every envelope that traverses a non-trivial route (≥1 hop) records a
``RouteHop`` row at each server it visits. Aggregated by ``trace_id``,
the rows reconstruct the full causal chain:

  trace_01HX...    P0 group_call_signal_offer   100 hops
    ├ span_001  server_001  hop=0  forwarded_to=server_002 ack=5ms
    ├ span_002  server_002  hop=1  forwarded_to=server_003 ack=4ms
    ├ ...
    └ span_100  server_100  delivered=user_B    total=850ms

Why a separate table
--------------------
``RouteTrace`` and ``RouteHop`` could live as a single denormalized
table, but routing decisions vs. trace metadata have different
retention requirements: hops are high-volume and trim aggressively
(default 7 days); trace summaries stay longer for incident review.

Cardinality
-----------
Production: ≤3 hops per envelope, hundreds of envelopes per second.
Chaos mode: 100 hops per envelope, dozens per minute. Either way,
TTL-based cleanup keeps the table bounded — see
``trace_collector_service.reaper_loop``.
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utc_now


class RouteTrace(Base, TimestampMixin):
    """One row per traced envelope (trace_id is the natural key)."""

    __tablename__ = "route_traces"

    # trace_id IS the PK — they're ULIDs, naturally sortable.
    trace_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(2), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="production")
    # production | chaos_chain

    source_server_id: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_server_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Outcome: "delivered" | "loop" | "expired" | "max_hops" | "dlq" | "in_flight"
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="in_flight")

    # Aggregate metrics filled in by trace_collector when complete.
    total_hops: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optional context.
    call_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(48), nullable=True)

    hops: Mapped[list["RouteHop"]] = relationship(
        "RouteHop",
        back_populates="trace",
        cascade="all, delete-orphan",
        order_by="RouteHop.hop_index",
    )

    __table_args__ = (
        Index("ix_route_traces_started_at", "started_at"),
        Index("ix_route_traces_outcome_started", "outcome", "started_at"),
    )


class RouteHop(Base, TimestampMixin):
    """One row per server-touch on an envelope's path."""

    __tablename__ = "route_hops"

    # Synthetic PK because (trace_id, hop_index) might collide if a
    # retry rotates spans — we keep both attempts separately for
    # post-mortem.
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        String(48),
        ForeignKey("route_traces.trace_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    span_id: Mapped[str] = mapped_column(String(48), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(48), nullable=True)

    server_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hop_index: Mapped[int] = mapped_column(Integer, nullable=False)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    forwarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ack_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    next_server_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "forwarded" | "delivered" | "dlq" | "expired" | "loop" | "ack_only"
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="forwarded")
    # Optional opaque metadata (e.g. error message, retry count).
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    trace: Mapped["RouteTrace"] = relationship("RouteTrace", back_populates="hops")

    __table_args__ = (
        Index("ix_route_hops_trace_index", "trace_id", "hop_index"),
        Index("ix_route_hops_received_at", "received_at"),
    )
