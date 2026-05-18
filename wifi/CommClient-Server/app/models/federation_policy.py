"""
Federation routing policies.

A policy row describes a deterministic routing rule for outbound
federation envelopes:

    match  — JSON predicate (kind, channel-regex, sender-regex, region, …)
    action — JSON action (route_to: [server_ids], fallback: [...],
             blackhole: bool, require_trust: trusted|peer|…, drop_above_rtt_ms: int)
    priority — lower number wins; first match short-circuits.

Policies are evaluated by ``FederationPolicyEngine``. The engine
exposes a ``simulate`` mode that performs the same evaluation against
an arbitrary envelope without producing side effects.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FederationPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "federation_policies"
    __table_args__ = (
        Index("ix_fed_policy_priority", "priority"),
        Index("ix_fed_policy_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    action: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederationPolicy {self.name} prio={self.priority}>"
