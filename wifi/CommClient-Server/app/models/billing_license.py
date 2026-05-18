"""
Phase 7 / Module AG-EXT — Tenancy + Billing Portal extension models.

This module ADDS new tables for the admin billing portal WITHOUT touching
the existing :mod:`app.models.billing` rows.  Five new tables:

    billing_licenses              — operator-signed Ed25519 license blobs
                                    (offline activation, per-tenant entitlements)
    billing_license_revocations   — CRL of revoked license keys
    billing_plan_audit            — change history for plan upserts/removals
    tenant_admin_sessions         — short-lived impersonation tokens minted
                                    by ``POST /tenants/{id}/impersonate``
    rbac_user_password_resets     — admin-initiated temporary passwords
                                    (single-use, expires in 60 minutes)

Tenancy: every license carries ``workspace_id`` (we treat workspaces as
the tenant). Revocations are global. Plan audit rows are global. Tenant
admin sessions reference both the issuer and the impersonated workspace.

Hash-chain note:
Each license payload (without the signature) is hashed with SHA-256 and
stored in ``payload_sha256`` so we can verify the on-disk bytes match
what was signed without re-decoding base64.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_LICENSE_STATUSES = ("active", "expired", "revoked", "suspended")


def _license_key() -> str:
    """Generate a license key shaped like ``HLN-XXXX-XXXX-XXXX-XXXX``
    (20 hex digits, ~80 bits of entropy)."""
    raw = secrets.token_hex(10).upper()
    parts = [raw[i : i + 4] for i in range(0, 20, 4)]
    return "HLN-" + "-".join(parts)


def _admin_token() -> str:
    return secrets.token_urlsafe(40)


def _temp_password() -> str:
    """A human-readable temporary password (3 hex groups + digits)."""
    return (
        secrets.token_hex(2).upper()
        + "-"
        + secrets.token_hex(2).upper()
        + "-"
        + str(secrets.randbelow(9000) + 1000)
    )


# ───────────────────────────────────────────────────────────────────────
# License
# ───────────────────────────────────────────────────────────────────────


class BillingLicense(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Operator-issued offline license bound to a tenant (workspace).

    The canonical bytes are stored in ``payload_json`` (compact JSON) and
    signed with the operator's Ed25519 private key (see
    :class:`app.services.billing.license_signer.LicenseSigner`).
    """

    __tablename__ = "billing_licenses"
    __table_args__ = (
        UniqueConstraint("license_key", name="uq_billing_licenses_key"),
        Index("ix_billing_licenses_workspace_id", "workspace_id"),
        Index("ix_billing_licenses_status", "status"),
        Index("ix_billing_licenses_expires_at", "expires_at"),
    )

    license_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, default=_license_key,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    seats: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    signature_b64: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    issued_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )

    @property
    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_valid(self) -> bool:
        return (
            self.status == "active"
            and not self.is_expired
            and not self.is_revoked
        )


# ───────────────────────────────────────────────────────────────────────
# License Revocation (CRL row, kept for forensic history)
# ───────────────────────────────────────────────────────────────────────


class LicenseRevocation(Base, UUIDPrimaryKeyMixin):
    """One row per revoked license key. We keep the BillingLicense row
    around (status='revoked'), but also write an immutable CRL entry so
    that even if the license row is deleted by a future migration, the
    revocation history survives."""

    __tablename__ = "billing_license_revocations"
    __table_args__ = (
        UniqueConstraint("license_key", name="uq_billing_revocations_key"),
        Index("ix_billing_revocations_revoked_at", "revoked_at"),
    )

    license_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True,
    )
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    revoked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ───────────────────────────────────────────────────────────────────────
# Plan Audit
# ───────────────────────────────────────────────────────────────────────


class PlanAuditEntry(Base, UUIDPrimaryKeyMixin):
    """Immutable record of every plan write through the admin API.

    The diff is stored as two JSON blobs (``before``/``after``); a NULL
    in either indicates create/delete respectively.
    """

    __tablename__ = "billing_plan_audit"
    __table_args__ = (
        Index("ix_billing_plan_audit_slug", "plan_slug"),
        Index("ix_billing_plan_audit_actor", "actor_id"),
        Index("ix_billing_plan_audit_at", "occurred_at"),
    )

    plan_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ───────────────────────────────────────────────────────────────────────
# Tenant impersonation sessions
# ───────────────────────────────────────────────────────────────────────


class TenantAdminSession(Base, UUIDPrimaryKeyMixin):
    """Short-lived admin token granting access AS a tenant. Used by
    `POST /api/admin/tenants/{id}/impersonate`. Default TTL is 15 minutes
    — enough to investigate a support ticket, short enough that a
    stolen token decays quickly.
    """

    __tablename__ = "tenant_admin_sessions"
    __table_args__ = (
        UniqueConstraint("token", name="uq_tenant_admin_sessions_token"),
        Index("ix_tenant_admin_sessions_workspace_id", "workspace_id"),
        Index("ix_tenant_admin_sessions_expires_at", "expires_at"),
    )

    token: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, default=_admin_token,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    issued_by: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: utc_now() + timedelta(minutes=15),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at


# ───────────────────────────────────────────────────────────────────────
# Password resets
# ───────────────────────────────────────────────────────────────────────


class RbacPasswordReset(Base, UUIDPrimaryKeyMixin):
    """An admin-triggered temporary password.

    Generated by ``POST /api/admin/rbac/users/{id}/reset-password``; the
    temporary value is shown ONCE in the response, then only its hash
    lives in the DB. Expires after 60 minutes by default.
    """

    __tablename__ = "rbac_user_password_resets"
    __table_args__ = (
        Index("ix_rbac_pw_resets_user_id", "user_id"),
        Index("ix_rbac_pw_resets_expires_at", "expires_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    temp_password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: utc_now() + timedelta(minutes=60),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
