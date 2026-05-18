"""
Phase 6 / Module AC — High Availability & Clustering models.

Two tables:
    cluster_nodes        — every Helen-Server instance in the cluster
    cluster_leader_elect — current leader lease row (single-row guard)

Both tables are append-light / update-heavy; nodes heartbeat to refresh
``last_seen`` and the leader heartbeats ``expires_at``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_CLUSTER_NODE_STATUSES = ("joining", "active", "draining", "down")
VALID_CLUSTER_NODE_ROLES = ("primary", "replica", "observer")


class ClusterNode(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per Helen-Server instance currently in the cluster.

    The ``node_id`` is a stable identifier (typically a UUID written to
    disk at first start) that survives restarts, while the primary key
    ``id`` is a fresh UUID for every registration row.
    """

    __tablename__ = "cluster_nodes"
    __table_args__ = (
        UniqueConstraint("node_id", name="uq_cluster_nodes_node_id"),
        Index("ix_cluster_nodes_status", "status"),
        Index("ix_cluster_nodes_last_seen", "last_seen"),
    )

    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    advertise_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="joining",
        server_default="joining",
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="replica",
        server_default="replica",
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return (
            f"<ClusterNode {self.node_id[:8]} {self.hostname} "
            f"{self.status}/{self.role}>"
        )


class LeaderElection(Base, UUIDPrimaryKeyMixin):
    """Leader lease row. Only one row should be active at any moment;
    the (term, leader_node_id, lock_token) triple proves ownership.

    On take-over the new leader bumps ``term`` and rewrites the row.
    """

    __tablename__ = "cluster_leader_elect"
    __table_args__ = (
        Index("ix_cluster_leader_term", "term"),
        Index("ix_cluster_leader_expires", "expires_at"),
    )

    term: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    leader_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    lock_token: Mapped[str] = mapped_column(String(128), nullable=False)

    def __repr__(self) -> str:                                  # pragma: no cover
        return (
            f"<LeaderElection term={self.term} "
            f"node={self.leader_node_id[:8]}>"
        )
