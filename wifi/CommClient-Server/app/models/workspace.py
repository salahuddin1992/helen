"""
Phase 3 / Module M — Multi-Tenant Workspaces.

Three tables:
    workspaces          — top-level tenant container
    workspace_members   — user ↔ workspace association with role
    workspace_invites   — single-use invite codes (optionally email-bound)

Tenancy isolation is enforced at the ORM-query layer via
``app.services.tenancy.tenant_scope.apply_tenant_filter``; this model only
declares the relationships and constraints.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


def _invite_code() -> str:
    """Generate a 32-char URL-safe invite code (≈ 192 bits of entropy)."""
    return secrets.token_urlsafe(24)[:32]


def _slug_default() -> str:
    return secrets.token_hex(6)


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Top-level tenant container. Every domain object SHOULD be scoped by a
    ``workspace_id`` column once we migrate them; legacy rows without one
    fall into the implicit "default" workspace handled by the bridge."""
    __tablename__ = "workspaces"
    __table_args__ = (
        Index("ix_workspaces_owner_id", "owner_id"),
    )

    slug: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, default=_slug_default,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    plan: Mapped[str] = mapped_column(
        String(32), nullable=False, default="free", server_default="free",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    members: Mapped[list["WorkspaceMember"]] = relationship(
        "WorkspaceMember", back_populates="workspace",
        cascade="all, delete-orphan", lazy="selectin",
    )
    invites: Mapped[list["WorkspaceInvite"]] = relationship(
        "WorkspaceInvite", back_populates="workspace",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"<Workspace {self.slug} name={self.name!r}>"


class WorkspaceMember(Base, UUIDPrimaryKeyMixin):
    """User membership inside a workspace. ``role`` is one of:
    owner / admin / member / viewer."""
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        Index("ix_workspace_members_workspace_id", "workspace_id"),
        Index("ix_workspace_members_user_id", "user_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="member", server_default="member",
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    invited_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    workspace: Mapped[Workspace] = relationship(
        "Workspace", back_populates="members",
    )


class WorkspaceInvite(Base, UUIDPrimaryKeyMixin):
    """Single-use invite code. ``email`` is optional — when set, only that
    address can redeem the invite (after their first OAuth/local login)."""
    __tablename__ = "workspace_invites"
    __table_args__ = (
        Index("ix_workspace_invites_workspace_id", "workspace_id"),
        Index("ix_workspace_invites_code", "code", unique=True),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, default=_invite_code,
    )
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="member", server_default="member",
    )
    issued_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: utc_now() + timedelta(hours=72),
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    used_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    workspace: Mapped[Workspace] = relationship(
        "Workspace", back_populates="invites",
    )

    @property
    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.used_at is not None
