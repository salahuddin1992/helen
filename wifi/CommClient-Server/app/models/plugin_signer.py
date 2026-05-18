"""
Phase 7 / Module AH — Verified Signer public keys.

The file-based trust store at ``data/plugin-trusted-keys.json`` is the
canonical source consumed by :mod:`signer`. We also mirror entries into
this table so the admin UI can query them via the standard ORM and join
to events / audit. Adding a signer writes to BOTH stores.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VerifiedSigner(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "plugin_verified_signers"
    __table_args__ = (
        UniqueConstraint("name", name="uq_plugin_signer_name"),
        Index("ix_plugin_signer_name", "name"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ed25519",
        server_default="ed25519",
    )
    fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    added_by: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
