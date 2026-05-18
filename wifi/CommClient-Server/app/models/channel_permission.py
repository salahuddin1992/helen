"""
Granular per-channel permissions.

Two layers:

1. **Role defaults**: ChannelRolePermission rows define which permission names
   are granted to a given role (admin/moderator/member) on a specific channel.
   When no row exists, sensible defaults are used (see DEFAULT_ROLE_PERMS in
   permission_service.py).

2. **Per-member overrides**: ChannelMemberPermission rows grant or revoke a
   specific permission for a single member, taking priority over the role
   default.

This lets channel owners go from "admin can do everything, member can post"
toward fine-grained policies (e.g. "members can pin but not mention everyone")
without rewriting the existing role column.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelRolePermission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "channel_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "role",
            "permission",
            name="uq_channel_role_permission",
        ),
        Index("ix_channel_role_perms_channel", "channel_id"),
    )

    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    permission: Mapped[str] = mapped_column(String(48), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    channel: Mapped["Channel"] = relationship("Channel")  # noqa: F821

    def __repr__(self) -> str:
        sign = "+" if self.granted else "-"
        return (
            f"<ChannelRolePermission ch={self.channel_id[:8]} {self.role} "
            f"{sign}{self.permission}>"
        )


class ChannelMemberPermission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "channel_member_permissions"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "user_id",
            "permission",
            name="uq_channel_member_permission",
        ),
        Index("ix_channel_member_perms_user", "channel_id", "user_id"),
    )

    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission: Mapped[str] = mapped_column(String(48), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    channel: Mapped["Channel"] = relationship("Channel")  # noqa: F821
    user: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        sign = "+" if self.granted else "-"
        return (
            f"<ChannelMemberPermission ch={self.channel_id[:8]} "
            f"user={self.user_id[:8]} {sign}{self.permission}>"
        )
