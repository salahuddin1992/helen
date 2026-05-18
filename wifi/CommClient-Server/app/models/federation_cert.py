"""
Federation per-peer mTLS certificates.

Tracks the active leaf certificate, fingerprint, chain depth, issuer
and lifecycle for every federated peer.  ``FederationPeerMeta.cert_id``
points at the active row.

Rotation pattern
----------------
1. ``rotate(peer_id)`` issues a new keypair + CSR, marks the new row
   ``active=True`` and the previous one ``active=False, revoked_at=now``.
2. The new fingerprint is propagated to the peer through the regular
   handshake.
3. Old rows are kept for the audit window (default 90 days).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

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

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FederationCert(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "federation_certs"
    __table_args__ = (
        Index("ix_fed_cert_server", "server_id"),
        Index("ix_fed_cert_active", "active"),
        Index("ix_fed_cert_expires", "not_after"),
    )

    server_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    issuer: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    serial: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    not_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    chain_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    chain_pem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    leaf_pem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rotation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FederationCert {self.server_id} fp={self.fingerprint_sha256[:12]}>"
