"""
Phase 7 / Module AL — Zero-Trust Networking models.

Six tables:
    zt_workload_identities      — SPIFFE-style workload identities
    zt_device_attestations      — endpoint posture records
    zt_access_policies          — declarative authz policy
    zt_access_requests          — decision log
    zt_jit_grants               — just-in-time elevated access
    zt_continuous_assessments   — running session risk evaluations
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


VALID_ZT_WORKLOAD_KINDS = ("service", "user", "device", "agent")


class WorkloadIdentity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """SPIFFE-style workload identity.

    ``spiffe_id`` is the canonical identifier; format::
        spiffe://helen/<workload_type>/<name>
    """

    __tablename__ = "zt_workload_identities"
    __table_args__ = (
        UniqueConstraint("spiffe_id", name="uq_zt_workload_spiffe"),
        Index("ix_zt_workload_kind", "workload_type"),
        Index("ix_zt_workload_parent", "parent_identity_id"),
        Index("ix_zt_workload_expires", "expires_at"),
    )

    spiffe_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workload_type: Mapped[str] = mapped_column(String(32), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    parent_identity_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey("zt_workload_identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )


class DeviceAttestation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Endpoint posture snapshot."""

    __tablename__ = "zt_device_attestations"
    __table_args__ = (
        Index("ix_zt_device_user", "user_id"),
        Index("ix_zt_device_device", "device_id"),
        Index("ix_zt_device_attested", "attested_at"),
        Index("ix_zt_device_valid", "valid_until"),
    )

    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    os: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    os_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    app_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    disk_encrypted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    screen_lock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    antivirus_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    jailbroken: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    attested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AccessPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Declarative access policy. First-match by ``priority`` order."""

    __tablename__ = "zt_access_policies"
    __table_args__ = (
        Index("ix_zt_policy_priority", "priority"),
        Index("ix_zt_policy_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_selector: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    resource_selector: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    allow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    conditions: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    obligations: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class AccessRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Decision log row — one per evaluation."""

    __tablename__ = "zt_access_requests"
    __table_args__ = (
        Index("ix_zt_access_session", "session_id"),
        Index("ix_zt_access_decided", "decided_at"),
        Index("ix_zt_access_decision", "decision"),
        Index("ix_zt_access_subject", "requester_identity"),
    )

    requester_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    reasons: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    obligations: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class JITGrant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Just-in-time elevated grant — time-boxed, approval-gated."""

    __tablename__ = "zt_jit_grants"
    __table_args__ = (
        Index("ix_zt_jit_user", "user_id"),
        Index("ix_zt_jit_expires", "expires_at"),
        Index("ix_zt_jit_status", "status"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    granted_by: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    granted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )


class ContinuousAssessment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Session-level risk / verification checkpoint."""

    __tablename__ = "zt_continuous_assessments"
    __table_args__ = (
        Index("ix_zt_assess_session", "session_id"),
        Index("ix_zt_assess_kind", "check_kind"),
        Index("ix_zt_assess_eval", "evaluated_at"),
    )

    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    check_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
