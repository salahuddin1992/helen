"""
Audit SIEM — Alert Rule model.

Each row is an operator-defined detection rule. The rule's ``condition_dsl``
is a small expression language (see ``app.services.audit.alert_rules``)
that fires on matching audit chain entries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_RULE_SEVERITIES = ("info", "low", "medium", "high", "critical")
VALID_ALERT_CHANNELS = ("email", "webhook", "slack", "local", "sms")


class AuditAlertRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_alert_rules"
    __table_args__ = (
        Index("ix_audit_alert_rules_enabled", "enabled"),
        Index("ix_audit_alert_rules_severity", "severity"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    condition_dsl: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium", server_default="medium",
    )
    channels: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    hit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "condition_dsl": self.condition_dsl,
            "severity": self.severity,
            "channels": list(self.channels or []),
            "enabled": bool(self.enabled),
            "hit_count": int(self.hit_count or 0),
            "last_hit_at": self.last_hit_at.isoformat() if self.last_hit_at else None,
            "created_by": self.created_by,
            "extra": dict(self.extra or {}),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
