"""
Phase 7 / Module AH — Marketplace & Plugin System models.

Five tables modelling the lifecycle of an external plugin:

    plugin_manifests             — global registry of plugin versions
    plugin_installations         — per-workspace installs
    plugin_permission_grants     — explicit user consent per permission
    plugin_events                — execution / lifecycle audit trail
    plugin_marketplace_listings  — marketplace visibility metadata
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
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_INSTALL_STATUSES = ("installed", "disabled", "error", "uninstalling")
VALID_LISTING_STATUSES = ("draft", "pending", "approved", "rejected", "deprecated")
VALID_PLUGIN_EVENTS = (
    "hook_called", "hook_error", "error", "install", "uninstall",
    "enable", "disable", "config_changed", "permission_granted",
    "permission_revoked", "signature_failed", "sandbox_violation",
)


# ───────────────────────────────────────────────────────────────────────
# PluginManifest
# ───────────────────────────────────────────────────────────────────────


class PluginManifest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "plugin_manifests"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_plugin_slug_version"),
        Index("ix_plugin_manifests_slug", "slug"),
        Index("ix_plugin_manifests_published_at", "published_at"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    author: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    homepage: Mapped[str | None] = mapped_column(String(512), nullable=True)
    min_helen_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_helen_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permissions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    entrypoint: Mapped[str] = mapped_column(String(256), nullable=False)
    code_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    code_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    signed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hooks_subscribed: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    ui_routes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    settings_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    dependencies: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    installations: Mapped[list["PluginInstallation"]] = relationship(
        "PluginInstallation", back_populates="manifest",
        cascade="all, delete-orphan", lazy="noload",
    )


# ───────────────────────────────────────────────────────────────────────
# PluginInstallation
# ───────────────────────────────────────────────────────────────────────


class PluginInstallation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "plugin_installations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "manifest_id",
                         name="uq_plugin_install_ws_manifest"),
        Index("ix_plugin_install_workspace_id", "workspace_id"),
        Index("ix_plugin_install_status", "status"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("plugin_manifests.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="installed", server_default="installed",
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    installed_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_invoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    manifest: Mapped[PluginManifest] = relationship(
        "PluginManifest", back_populates="installations",
    )
    grants: Mapped[list["PluginPermissionGrant"]] = relationship(
        "PluginPermissionGrant", back_populates="installation",
        cascade="all, delete-orphan", lazy="selectin",
    )


# ───────────────────────────────────────────────────────────────────────
# PluginPermissionGrant
# ───────────────────────────────────────────────────────────────────────


class PluginPermissionGrant(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "plugin_permission_grants"
    __table_args__ = (
        UniqueConstraint("installation_id", "permission",
                         name="uq_plugin_grant_install_perm"),
        Index("ix_plugin_grants_installation_id", "installation_id"),
    )

    installation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("plugin_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission: Mapped[str] = mapped_column(String(64), nullable=False)
    granted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    granted_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    installation: Mapped[PluginInstallation] = relationship(
        "PluginInstallation", back_populates="grants",
    )


# ───────────────────────────────────────────────────────────────────────
# PluginEvent
# ───────────────────────────────────────────────────────────────────────


class PluginEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "plugin_events"
    __table_args__ = (
        Index("ix_plugin_events_installation_id", "installation_id"),
        Index("ix_plugin_events_event", "event"),
        Index("ix_plugin_events_occurred_at", "occurred_at"),
    )

    installation_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("plugin_installations.id", ondelete="CASCADE"),
        nullable=True,
    )
    manifest_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("plugin_manifests.id", ondelete="SET NULL"),
        nullable=True,
    )
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )


# ───────────────────────────────────────────────────────────────────────
# MarketplaceListing
# ───────────────────────────────────────────────────────────────────────


class MarketplaceListing(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "plugin_marketplace_listings"
    __table_args__ = (
        UniqueConstraint("manifest_id", name="uq_plugin_listing_manifest"),
        Index("ix_plugin_listings_status", "listing_status"),
        Index("ix_plugin_listings_category", "category"),
        Index("ix_plugin_listings_featured", "featured"),
    )

    manifest_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("plugin_manifests.id", ondelete="CASCADE"),
        nullable=False,
    )
    listing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft",
    )
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rating_avg: Mapped[float] = mapped_column(
        Numeric(3, 2), nullable=False, default=0, server_default="0",
    )
    ratings_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    downloads: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    screenshots: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    tags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    long_description: Mapped[str | None] = mapped_column(Text, nullable=True)
