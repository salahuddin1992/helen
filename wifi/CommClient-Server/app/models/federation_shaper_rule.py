"""
Federation bandwidth shaper rules.

Token-bucket parameters per peer:

    in_kbps    — sustained ingress rate
    out_kbps   — sustained egress rate
    burst_kbps — instantaneous burst tolerance
    priority   — 0 (best-effort) … 7 (real-time)
    preset     — equal | region | role | custom

A peer has 0..1 active rules; ``FederationPeerMeta.shaper_rule_id``
points at the live row. Historic rows are kept (``active=False``) for
audit and rollback.
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


VALID_SHAPER_PRESETS = ("equal", "region", "role", "custom")


class FederationShaperRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "federation_shaper_rules"
    __table_args__ = (
        Index("ix_fed_shaper_server", "server_id"),
        Index("ix_fed_shaper_active", "active"),
    )

    server_id: Mapped[str] = mapped_column(String(255), nullable=False)
    preset: Mapped[str] = mapped_column(
        String(16), nullable=False, default="custom", server_default="custom",
    )
    in_kbps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_kbps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    burst_kbps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4",
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<FederationShaperRule {self.server_id} "
            f"in={self.in_kbps} out={self.out_kbps} prio={self.priority}>"
        )
