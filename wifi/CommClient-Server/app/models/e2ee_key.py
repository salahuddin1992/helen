"""
End-to-End Encryption key management models.

X3DH key bundle components:
  - Identity Key: long-term user key (persistent)
  - Signed Pre-Key: medium-term (rotated periodically)
  - One-Time Pre-Keys: single-use (consumed on session init)
  - E2EE Session: metadata for encrypted conversations

Security properties:
  - Server never sees plaintext, only stores public keys
  - One-time keys are consumed atomically to prevent reuse
  - Pre-key rotation enables perfect forward secrecy
  - Session establishment records who initiated encryption
"""

from __future__ import annotations

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class IdentityKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    User's long-term identity key (X3DH ik).
    One per user, used to verify signature on pre-keys.
    """

    __tablename__ = "e2ee_identity_keys"

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    public_key: Mapped[bytes] = mapped_column(Text, nullable=False)  # Base64 encoded
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return f"<IdentityKey {self.user_id[:8]} v{self.key_version}>"


class SignedPreKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Medium-term pre-key (X3DH spk) signed by identity key.
    Rotated periodically, but server keeps last N versions for retransmission.
    """

    __tablename__ = "e2ee_signed_pre_keys"

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key_id: Mapped[int] = mapped_column(Integer, nullable=False)  # Version counter
    public_key: Mapped[bytes] = mapped_column(Text, nullable=False)  # Base64 encoded
    signature: Mapped[bytes] = mapped_column(Text, nullable=False)  # ik signature of spk
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship("User")

    __table_args__ = (UniqueConstraint("user_id", "key_id", name="uq_spk_user_version"),)

    def __repr__(self) -> str:
        return f"<SignedPreKey {self.user_id[:8]} #{self.key_id}>"


class OneTimePreKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Single-use pre-key (X3DH opk) consumed during session init.
    Batch uploaded and consumed atomically.
    Marked as used immediately on fetch (race-condition safe).
    """

    __tablename__ = "e2ee_one_time_pre_keys"

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key_id: Mapped[int] = mapped_column(Integer, nullable=False)  # Sequential ID
    public_key: Mapped[bytes] = mapped_column(Text, nullable=False)  # Base64 encoded
    used: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    used_by_user_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    used_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    used_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[used_by_user_id])

    __table_args__ = (UniqueConstraint("user_id", "key_id", name="uq_otpk_user_version"),)

    def __repr__(self) -> str:
        status = "used" if self.used else "unused"
        return f"<OneTimePreKey {self.user_id[:8]} #{self.key_id} {status}>"


class E2EESession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Metadata for an established encrypted session (Double Ratchet state).
    Initiator is the user who fetched the responder's bundle and started X3DH.
    Responder is the user whose bundle was fetched.

    Session state is maintained client-side (Double Ratchet); server only tracks metadata.
    This record helps detect stale/abandoned sessions and enables re-keying.
    """

    __tablename__ = "e2ee_sessions"

    session_id: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
        index=True,
    )
    initiator_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    responder_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    established_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    last_message_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)

    # Relationships
    initiator: Mapped["User"] = relationship("User", foreign_keys=[initiator_id])
    responder: Mapped["User"] = relationship("User", foreign_keys=[responder_id])

    def __repr__(self) -> str:
        return f"<E2EESession {self.session_id[:16]}... {self.initiator_id[:4]}->{self.responder_id[:4]}>"
