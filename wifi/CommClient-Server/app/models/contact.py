"""
Contact / buddy-list model.
Bidirectional contact relationships between users.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Contact(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "contact_id", name="uq_user_contact"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    contact_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_favorite: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    contact_user: Mapped["User"] = relationship("User", foreign_keys=[contact_id])

    def __repr__(self) -> str:
        return f"<Contact {self.user_id[:8]} -> {self.contact_id[:8]}>"
