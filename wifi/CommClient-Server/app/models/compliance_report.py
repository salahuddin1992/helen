"""
Compliance reports + schedules — Module AB.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_FRAMEWORKS = (
    "GDPR", "HIPAA", "SOC2", "ISO27001", "ISO27017",
    "NIST_800_53", "PCI_DSS", "FedRAMP",
    "SAUDI_NCA_ECC", "UAE_TDRA",
)

VALID_REPORT_FORMATS = ("json", "csv", "pdf")
VALID_REPORT_STATUSES = (
    "pending", "running", "ready", "failed", "expired",
)


class ComplianceReport(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_reports_v2"
    __table_args__ = (
        Index("ix_cmp_rep2_framework", "framework"),
        Index("ix_cmp_rep2_status", "status"),
        Index("ix_cmp_rep2_period_end", "period_end"),
    )

    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(
        String(16), nullable=False, default="json", server_default="json",
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )


class ComplianceReportSchedule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_report_schedules"
    __table_args__ = (
        Index("ix_cmp_repsched_enabled", "enabled"),
    )

    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pdf", server_default="pdf",
    )
    # cron expression OR an interval keyword (daily/weekly/monthly/quarterly)
    cadence: Mapped[str] = mapped_column(
        String(64), nullable=False, default="monthly", server_default="monthly",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_report_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recipients: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list,
    )
