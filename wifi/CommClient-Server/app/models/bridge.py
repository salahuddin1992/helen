"""
Phase 5 / Module Y — Bridge models (Discord / Telegram / Slack).

Three tables:
    bridge_configs     — one row per configured bridge instance
    bridge_messages    — audit/history log of every message crossing the bridge
    bridge_identities  — mapping between Helen users and remote-platform users

Bridges are workspace-scoped so each tenant manages their own connections.
"""
from __future__ import annotations

from datetime import datetime
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


VALID_BRIDGE_KINDS = ("discord", "telegram", "slack")
VALID_BRIDGE_DIRECTIONS = ("helen_to_remote", "remote_to_helen")
VALID_BRIDGE_STATUSES = ("queued", "sent", "delivered", "failed", "duplicate")


class BridgeConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Configuration record for a single bidirectional bridge."""
    __tablename__ = "bridge_configs"
    __table_args__ = (
        Index("ix_bridge_configs_workspace_id", "workspace_id"),
        Index("ix_bridge_configs_kind", "kind"),
        UniqueConstraint(
            "workspace_id", "kind", "channel_helen_id", "channel_remote_id",
            name="uq_bridge_workspace_kind_channels",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    channel_helen_id: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )
    channel_remote_id: Mapped[str] = mapped_column(
        String(128), nullable=False,
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    last_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    messages: Mapped[list["BridgeMessage"]] = relationship(
        "BridgeMessage", back_populates="bridge",
        cascade="all, delete-orphan", lazy="noload",
    )
    identities: Mapped[list["BridgeIdentity"]] = relationship(
        "BridgeIdentity", back_populates="bridge",
        cascade="all, delete-orphan", lazy="noload",
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"<BridgeConfig {self.kind}:{self.name} ws={self.workspace_id}>"


class BridgeMessage(Base, UUIDPrimaryKeyMixin):
    """Audit row for every message that crossed a bridge in either direction."""
    __tablename__ = "bridge_messages"
    __table_args__ = (
        Index("ix_bridge_messages_bridge_id", "bridge_id"),
        Index("ix_bridge_messages_helen_msg", "helen_message_id"),
        Index("ix_bridge_messages_remote_msg", "remote_message_id"),
        Index("ix_bridge_messages_created_at", "created_at"),
    )

    bridge_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bridge_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    helen_message_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    remote_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    direction: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued",
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    bridge: Mapped[BridgeConfig] = relationship(
        "BridgeConfig", back_populates="messages",
    )


class BridgeIdentity(Base, UUIDPrimaryKeyMixin):
    """Mapping between a Helen user and a remote-platform user identity for
    a specific bridge instance."""
    __tablename__ = "bridge_identities"
    __table_args__ = (
        UniqueConstraint(
            "bridge_id", "remote_user_id",
            name="uq_bridge_identity_remote",
        ),
        Index("ix_bridge_identities_bridge_id", "bridge_id"),
        Index("ix_bridge_identities_helen_user", "helen_user_id"),
    )

    bridge_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bridge_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    helen_user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    remote_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_username: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    bridge: Mapped[BridgeConfig] = relationship(
        "BridgeConfig", back_populates="identities",
    )
