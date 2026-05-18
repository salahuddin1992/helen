"""
Phase 7 / Module AK — Edge computing models.

Four tables:
    edge_nodes        — geo-distributed edge instances
    edge_regions      — region metadata + data residency flags
    edge_routes       — workspace → edge_node priority routes
    edge_region_policies — per-workspace residency policies
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
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


VALID_EDGE_NODE_STATUSES = ("active", "draining", "down", "maintenance")
VALID_LATENCY_ZONES = ("hot", "warm", "cold")


class EdgeRegion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Logical region — e.g. ``eu-west-1``, ``us-east-1``."""

    __tablename__ = "edge_regions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_edge_regions_code"),
        Index("ix_edge_regions_country", "country"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    data_residency_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    gdpr_compliant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    latency_zone: Mapped[str] = mapped_column(
        String(16), nullable=False, default="warm", server_default="warm",
    )


class EdgeNode(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single edge worker instance."""

    __tablename__ = "edge_nodes"
    __table_args__ = (
        UniqueConstraint("node_id", name="uq_edge_nodes_node_id"),
        Index("ix_edge_nodes_region", "region"),
        Index("ix_edge_nodes_status", "status"),
        Index("ix_edge_nodes_heartbeat", "last_heartbeat"),
    )

    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    datacenter: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    advertise_url: Mapped[str] = mapped_column(String(512), nullable=False)
    public_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    geo_lat: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    geo_lng: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    capacity: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    current_load_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0",
    )
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EdgeNode {self.node_id} {self.region} {self.status}>"


class EdgeRoute(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Routing preference for a workspace toward an edge node."""

    __tablename__ = "edge_routes"
    __table_args__ = (
        Index("ix_edge_routes_workspace", "source_workspace_id"),
        Index("ix_edge_routes_node", "edge_node_id"),
        UniqueConstraint(
            "source_workspace_id", "edge_node_id",
            name="uq_edge_routes_ws_node",
        ),
    )

    source_workspace_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True,
    )
    edge_node_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("edge_nodes.id", ondelete="CASCADE"), nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    current_load_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0",
    )


class RegionPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-workspace data residency / region restriction."""

    __tablename__ = "edge_region_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_edge_policy_workspace"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
    )
    allowed_regions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    required_residency_region: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    encryption_at_rest_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    audit_log_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
