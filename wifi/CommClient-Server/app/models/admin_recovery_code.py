"""
AdminRecoveryCode — one-time-use recovery codes for the first admin.

Only the **hash** of each code is stored. Plaintext is shown to the
operator exactly once at generation time. When a code is consumed, the
row is marked ``used_at`` and cannot be replayed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class AdminRecoveryCode(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "admin_recovery_codes"

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True,
    )
    used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "used": self.used,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "used_ip": self.used_ip,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
