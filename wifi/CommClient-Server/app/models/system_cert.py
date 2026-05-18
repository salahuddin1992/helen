"""
SystemCert — operator-installed TLS material managed during onboarding.

This is distinct from the runtime TLS module (``app.core.tls``) which
re-reads disk paths on boot. The onboarding flow inserts a row here for
audit purposes and stores the cert/key payloads encrypted at rest.

A ``role`` column distinguishes:
    server   — primary server cert (helen.crt)
    root     — root CA cert exposed to clients for trust install
    intermediate
    operator — operator-imported cert (alternative to self-signed)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class SystemCert(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "system_certs"

    role: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, default="server",
    )
    key_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="rsa",
    )  # rsa | ed25519 | ecdsa
    common_name: Mapped[str] = mapped_column(String(255), nullable=False)
    san_list: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    fingerprint_sha256: Mapped[str] = mapped_column(
        String(95), nullable=False, index=True,
    )
    serial_number: Mapped[str] = mapped_column(String(64), nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    not_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    # Key is stored encrypted; the cert_manager handles envelope crypto.
    key_pem_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_self_signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True,
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    def to_dict(self, *, include_pem: bool = False) -> dict[str, Any]:
        out = {
            "id": self.id,
            "role": self.role,
            "key_type": self.key_type,
            "common_name": self.common_name,
            "san_list": list(self.san_list or []),
            "fingerprint_sha256": self.fingerprint_sha256,
            "serial_number": self.serial_number,
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "is_self_signed": self.is_self_signed,
            "active": self.active,
        }
        if include_pem:
            out["cert_pem"] = self.cert_pem
        return out
