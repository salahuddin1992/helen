"""
Phase 2 / Module G — Granular Role-Based Access Control models.

Tables
------
roles            — named role: superadmin / admin / moderator / member / guest …
permissions      — atomic permission keys ("messages.read", "system.config_write" …)
role_permissions — many-to-many between roles and permissions
user_roles       — assigns roles to users

System roles (``is_system=True``) cannot be deleted via the API. They are
seeded at first boot by ``app.services.rbac.registry.bootstrap_default_roles``.

The legacy ``users.role`` string column is left untouched — the new system
runs *alongside* it. Effective permissions for any user are the union of:
    1) permissions implied by the legacy ``users.role`` column
       (mapped to the new system role of the same name), plus
    2) every permission attached through ``user_roles → role_permissions``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class Role(Base, UUIDPrimaryKeyMixin):
    """Named role bag of permissions."""
    __tablename__ = "rbac_roles"

    name: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )

    permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="role",
        cascade="all, delete-orphan", lazy="selectin",
    )
    user_assignments: Mapped[list["UserRole"]] = relationship(
        "UserRole", back_populates="role",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"<Role {self.name} sys={self.is_system}>"


class Permission(Base, UUIDPrimaryKeyMixin):
    """Atomic permission. Key uses dotted notation: ``<category>.<verb>``."""
    __tablename__ = "rbac_permissions"

    key: Mapped[str] = mapped_column(
        String(96), unique=True, nullable=False, index=True,
    )
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    role_links: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="permission",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"<Permission {self.key}>"


class RolePermission(Base):
    """M:N — a role grants a permission. ``granted=False`` is a future-proof
    explicit deny; today only ``granted=True`` rows are inserted."""
    __tablename__ = "rbac_role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id",
                         name="uq_rbac_role_permission"),
        Index("ix_rbac_rp_role", "role_id"),
        Index("ix_rbac_rp_perm", "permission_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                    default=lambda: __import__("uuid").uuid4().hex)
    role_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("rbac_roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("rbac_permissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    granted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )

    role: Mapped["Role"] = relationship(
        "Role", back_populates="permissions",
    )
    permission: Mapped["Permission"] = relationship(
        "Permission", back_populates="role_links", lazy="selectin",
    )


class UserRole(Base):
    """M:N — assigns a role to a user."""
    __tablename__ = "rbac_user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id",
                         name="uq_rbac_user_role"),
        Index("ix_rbac_ur_user", "user_id"),
        Index("ix_rbac_ur_role", "role_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                    default=lambda: __import__("uuid").uuid4().hex)
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("rbac_roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )
    assigned_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )

    role: Mapped["Role"] = relationship(
        "Role", back_populates="user_assignments", lazy="selectin",
    )
