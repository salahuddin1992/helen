"""
Federation Health Map — extended peer metadata.

This model complements ``FederatedServer`` (federation_v2) with the
operational fields that the Health-Map admin panel needs but that do
NOT belong on the canonical peer row:

    * ``hostname``, ``ip_address`` — last observed network identity.
    * ``region`` — geographic / logical zone the peer lives in.
    * ``role`` — master / follower / observer in the consensus mesh.
    * ``quarantined`` — soft isolation flag (no inbound traffic accepted).
    * ``shaper_rule_id`` — pointer to the active bandwidth shaper rule.
    * ``cert_id``       — pointer to the active mTLS certificate row.
    * ``metrics_window_sec`` — rolling-buffer retention.

We deliberately do NOT modify ``federation_v2_servers`` — that table is
considered part of the federation protocol contract. This is a side
table joined on ``server_id``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_FED_ROLES = ("master", "follower", "observer", "candidate")
VALID_HEALTH_STATES = ("healthy", "degraded", "down", "quarantined", "unknown")


class FederationPeerMeta(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Operator-facing extension of a ``FederatedServer`` row."""

    __tablename__ = "federation_peer_meta"
    __table_args__ = (
        UniqueConstraint("server_id", name="uq_fed_peer_meta_server"),
        Index("ix_fed_peer_meta_role", "role"),
        Index("ix_fed_peer_meta_region", "region"),
        Index("ix_fed_peer_meta_quarantined", "quarantined"),
    )

    server_id: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="follower", server_default="follower",
    )
    health_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown",
    )
    quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    quarantined_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quarantined_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    shaper_rule_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
    )
    cert_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
    )
    last_handshake_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_rtt_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_throughput_kbps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_loss_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederationPeerMeta {self.server_id} role={self.role}>"
