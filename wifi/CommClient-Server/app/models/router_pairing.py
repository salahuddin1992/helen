"""
RouterPairing — TOFU-confirmed link between this Helen server and a
physical/virtual router that brokers federated traffic.

Pairing is two-phase:
    1. ``POST /router/pair``         — fetch public key, return fingerprint
    2. ``POST /router/pair/confirm`` — operator types/clicks the fingerprint
                                       to acknowledge it (Trust-On-First-Use)

Only one ``status='confirmed'`` row exists at a time per ``router_url``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class RouterPairing(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "router_pairings"

    router_url: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True,
    )
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(
        String(95), nullable=False, index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True,
    )  # pending | confirmed | revoked
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_ping_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_ping_rtt_ms: Mapped[int | None] = mapped_column(nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, onupdate=utc_now,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "router_url": self.router_url,
            "fingerprint_sha256": self.fingerprint_sha256,
            "status": self.status,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "last_ping_at": self.last_ping_at.isoformat() if self.last_ping_at else None,
            "last_ping_rtt_ms": self.last_ping_rtt_ms,
            "capabilities": dict(self.capabilities or {}),
        }
