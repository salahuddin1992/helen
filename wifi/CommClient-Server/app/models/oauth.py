"""
Phase 3 / Module N — OAuth2 / OIDC account linkage models.

Tables:
    oauth_accounts  — one row per (user, provider, provider_user_id) tuple
    oauth_states    — short-lived authorization-request state with PKCE
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class OAuthAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A linked external identity (Google / Microsoft / GitHub / generic OIDC).
    The same local user may have multiple external accounts."""
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id",
                         name="uq_oauth_provider_user"),
        Index("ix_oauth_accounts_user_id", "user_id"),
        Index("ix_oauth_accounts_email", "email"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    raw_profile: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )


class OAuthState(Base, UUIDPrimaryKeyMixin):
    """Single-use authorization-request state for CSRF + PKCE binding.
    Inserted in ``start_authorization``, consumed in ``handle_callback``,
    purged by a background sweep after ``ttl_seconds`` (default 600)."""
    __tablename__ = "oauth_states"
    __table_args__ = (
        Index("ix_oauth_states_state", "state", unique=True),
    )

    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    code_verifier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    desktop: Mapped[str] = mapped_column(
        String(8), nullable=False, default="0", server_default="0",
    )
    nonce: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
