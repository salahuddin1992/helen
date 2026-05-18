"""
eDiscovery cases and evidence — Module AB.

A case is a folder grouping evidence collected during an investigation.
Evidence items are pointers into existing resources (messages, files,
calls, audit entries) tagged with privilege/responsiveness markers and
optional notes. Exports produce signed bundles consumable by external
review tools (legal-zip / EDRM-XML / PDF report).
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_CASE_STATUSES = ("open", "review", "frozen", "closed", "archived")
VALID_EVIDENCE_TAGS = (
    "privileged", "responsive", "relevant", "not_relevant",
    "key_evidence", "redacted",
)


class ComplianceCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_cases"
    __table_args__ = (
        Index("ix_cmp_case_status", "status"),
        Index("ix_cmp_case_owner", "owner_id"),
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    matter_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open",
    )
    owner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
    custodians: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    hold_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("compliance_holds.id", ondelete="SET NULL"),
        nullable=True,
    )

    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class ComplianceCaseEvidence(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_case_evidence"
    __table_args__ = (
        UniqueConstraint(
            "case_id", "resource_type", "resource_id",
            name="uq_cmp_case_evidence_resource",
        ),
        Index("ix_cmp_case_evidence_case_id", "case_id"),
        Index("ix_cmp_case_evidence_tag", "tag"),
    )

    case_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("compliance_cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag: Mapped[str] = mapped_column(
        String(32), nullable=False, default="relevant",
        server_default="relevant",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    added_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )


class ComplianceCaseExport(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_case_exports"
    __table_args__ = (
        Index("ix_cmp_case_export_case_id", "case_id"),
        Index("ix_cmp_case_export_status", "status"),
    )

    case_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("compliance_cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    actor_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
