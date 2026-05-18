"""Phase 7 / Module AH — Plugin & Marketplace tables.

Adds five tables:
    plugin_manifests
    plugin_installations
    plugin_permission_grants
    plugin_events
    plugin_marketplace_listings

Revision ID: helen_plugins_addon
Revises: helen_billing_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_plugins_addon"
down_revision = "helen_billing_addon"
branch_labels = ("helen_plugins_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_manifests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("homepage", sa.String(length=512), nullable=True),
        sa.Column("min_helen_version", sa.String(length=32), nullable=True),
        sa.Column("max_helen_version", sa.String(length=32), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("entrypoint", sa.String(length=256), nullable=False),
        sa.Column("code_url", sa.String(length=2048), nullable=True),
        sa.Column("code_sha256", sa.String(length=64), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("signed_by", sa.String(length=128), nullable=True),
        sa.Column("hooks_subscribed", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("ui_routes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("settings_schema", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("dependencies", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", "version", name="uq_plugin_slug_version"),
    )
    op.create_index("ix_plugin_manifests_slug", "plugin_manifests", ["slug"])
    op.create_index("ix_plugin_manifests_published_at", "plugin_manifests", ["published_at"])

    op.create_table(
        "plugin_installations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manifest_id", sa.String(length=32),
                  sa.ForeignKey("plugin_manifests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="installed"),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("installed_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_invoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "manifest_id",
                            name="uq_plugin_install_ws_manifest"),
    )
    op.create_index("ix_plugin_install_workspace_id", "plugin_installations", ["workspace_id"])
    op.create_index("ix_plugin_install_status", "plugin_installations", ["status"])

    op.create_table(
        "plugin_permission_grants",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("installation_id", sa.String(length=32),
                  sa.ForeignKey("plugin_installations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("granted_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("installation_id", "permission",
                            name="uq_plugin_grant_install_perm"),
    )
    op.create_index("ix_plugin_grants_installation_id",
                    "plugin_permission_grants", ["installation_id"])

    op.create_table(
        "plugin_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("installation_id", sa.String(length=32),
                  sa.ForeignKey("plugin_installations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("manifest_id", sa.String(length=32),
                  sa.ForeignKey("plugin_manifests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_plugin_events_installation_id", "plugin_events", ["installation_id"])
    op.create_index("ix_plugin_events_event", "plugin_events", ["event"])
    op.create_index("ix_plugin_events_occurred_at", "plugin_events", ["occurred_at"])

    op.create_table(
        "plugin_marketplace_listings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("manifest_id", sa.String(length=32),
                  sa.ForeignKey("plugin_manifests.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("listing_status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("rating_avg", sa.Numeric(3, 2), nullable=False, server_default="0"),
        sa.Column("ratings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("downloads", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("screenshots", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("long_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_plugin_listings_status", "plugin_marketplace_listings", ["listing_status"])
    op.create_index("ix_plugin_listings_category", "plugin_marketplace_listings", ["category"])
    op.create_index("ix_plugin_listings_featured", "plugin_marketplace_listings", ["featured"])


def downgrade() -> None:
    for ix in ("ix_plugin_listings_featured", "ix_plugin_listings_category",
               "ix_plugin_listings_status"):
        op.drop_index(ix, table_name="plugin_marketplace_listings")
    op.drop_table("plugin_marketplace_listings")

    for ix in ("ix_plugin_events_occurred_at", "ix_plugin_events_event",
               "ix_plugin_events_installation_id"):
        op.drop_index(ix, table_name="plugin_events")
    op.drop_table("plugin_events")

    op.drop_index("ix_plugin_grants_installation_id",
                  table_name="plugin_permission_grants")
    op.drop_table("plugin_permission_grants")

    for ix in ("ix_plugin_install_status", "ix_plugin_install_workspace_id"):
        op.drop_index(ix, table_name="plugin_installations")
    op.drop_table("plugin_installations")

    for ix in ("ix_plugin_manifests_published_at", "ix_plugin_manifests_slug"):
        op.drop_index(ix, table_name="plugin_manifests")
    op.drop_table("plugin_manifests")
