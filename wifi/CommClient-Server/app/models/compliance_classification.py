"""
Data classification rules and findings — Module AB.

ClassificationRule.kind is one of: regex / keyword / file_type / luhn.
ClassificationRule.action is one of: tag / block / encrypt / alert.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_RULE_KINDS = ("regex", "keyword", "file_type", "luhn")
VALID_RULE_ACTIONS = ("tag", "block", "encrypt", "alert")
VALID_RULE_SEVERITIES = ("info", "low", "medium", "high", "critical")


class ClassificationRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_classification_rules"
    __table_args__ = (
        Index("ix_cmp_clsrule_enabled", "enabled"),
        Index("ix_cmp_clsrule_severity", "severity"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tag", server_default="tag",
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium", server_default="medium",
    )
    classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pii", server_default="pii",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )


class ClassificationFinding(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_classification_findings"
    __table_args__ = (
        Index("ix_cmp_clsfind_resource", "resource_type", "resource_id"),
        Index("ix_cmp_clsfind_rule_id", "rule_id"),
        Index("ix_cmp_clsfind_severity", "severity"),
        Index("ix_cmp_clsfind_found_at", "found_at"),
    )

    rule_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("compliance_classification_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    field: Mapped[str | None] = mapped_column(String(128), nullable=True)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium",
    )
    confidence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=80, server_default="80",
    )
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    found_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    extras: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
