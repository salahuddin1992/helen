"""
Phase 6 / Module AE — Security models.

Four tables:
    ip_blocks            — active and expired blocklist entries
    login_attempts       — history of every login (success and failure)
    security_events      — generic event log (WAF / IDS / rotation / etc.)
    security_advisories  — OSV.dev advisories matched against requirements
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
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_SECURITY_EVENT_SEVERITIES = ("info", "warning", "high", "critical")
VALID_SECURITY_EVENT_KINDS = (
    "waf_block", "rate_limit", "ids_alert", "login_failure",
    "login_lockout", "ip_blocked", "ip_unblocked", "secret_rotated",
    "advisory_detected",
)


class IPBlock(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An IP range blocked at the application layer."""
    __tablename__ = "ip_blocks"
    __table_args__ = (
        Index("ix_ip_blocks_cidr", "ip_cidr"),
        Index("ix_ip_blocks_expires", "expires_at"),
    )

    ip_cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    blocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    blocked_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None,
    )


class LoginAttempt(Base, UUIDPrimaryKeyMixin):
    """One row per login attempt — feeds the IDS anomaly detector."""
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_username", "username"),
        Index("ix_login_attempts_ip", "ip"),
        Index("ix_login_attempts_attempted_at", "attempted_at"),
    )

    username: Mapped[str] = mapped_column(String(255), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )


class SecurityEvent(Base, UUIDPrimaryKeyMixin):
    """Generic security event — WAF blocks, IDS alerts, secret rotations,
    etc. The ``kind`` column is the discriminator."""
    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_kind", "kind"),
        Index("ix_security_events_severity", "severity"),
        Index("ix_security_events_created_at", "created_at"),
        Index("ix_security_events_ip", "ip"),
    )

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info",
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )


class SecurityAdvisory(Base, UUIDPrimaryKeyMixin):
    """An OSV.dev advisory matched against an installed package."""
    __tablename__ = "security_advisories"
    __table_args__ = (
        Index("ix_security_advisories_package", "package"),
        Index("ix_security_advisories_severity", "severity"),
        Index("ix_security_advisories_acknowledged", "acknowledged"),
    )

    package: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    cve: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown",
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fixed_in: Mapped[str | None] = mapped_column(String(128), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    acknowledged_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
