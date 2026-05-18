"""Phase 7 / Module AH — Plugin Marketplace Manager extension tables.

Adds three tables on top of ``helen_plugins_addon``:
    plugin_ratings
    plugin_verified_signers
    plugin_jobs

Revision ID: helen_plugins_marketplace_addon
Revises: helen_analytics_addon (was helen_plugins_addon)
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_plugins_marketplace_addon"
# Re-pointed off helen_analytics_addon to absorb that dangling head and
# linearize the post-9-block chain (see migrations/README.md).
down_revision = "helen_analytics_addon"
branch_labels = None
# Force the plugin_manifests/workspaces ancestor to be present even after
# the chain rewire.
depends_on = None


def upgrade() -> None:
    # ── plugin_ratings ────────────────────────────────────────
    op.create_table(
        "plugin_ratings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("manifest_slug", sa.String(length=64), nullable=False),
        sa.Column("manifest_id", sa.String(length=32),
                  sa.ForeignKey("plugin_manifests.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("review", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("manifest_slug", "user_id",
                            name="uq_plugin_rating_slug_user"),
    )
    op.create_index("ix_plugin_rating_slug",
                    "plugin_ratings", ["manifest_slug"])
    op.create_index("ix_plugin_rating_user",
                    "plugin_ratings", ["user_id"])

    # ── plugin_verified_signers ──────────────────────────────
    op.create_table(
        "plugin_verified_signers",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False,
                  server_default="ed25519"),
        sa.Column("fingerprint", sa.String(length=128), nullable=True),
        sa.Column("added_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_plugin_signer_name"),
    )
    op.create_index("ix_plugin_signer_name",
                    "plugin_verified_signers", ["name"])

    # ── plugin_jobs ──────────────────────────────────────────
    op.create_table(
        "plugin_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("phase", sa.String(length=32), nullable=True),
        sa.Column("pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actor_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_plugin_jobs_slug", "plugin_jobs", ["slug"])
    op.create_index("ix_plugin_jobs_state", "plugin_jobs", ["state"])
    op.create_index("ix_plugin_jobs_started_at", "plugin_jobs", ["started_at"])


def downgrade() -> None:
    for ix in ("ix_plugin_jobs_started_at", "ix_plugin_jobs_state",
               "ix_plugin_jobs_slug"):
        op.drop_index(ix, table_name="plugin_jobs")
    op.drop_table("plugin_jobs")

    op.drop_index("ix_plugin_signer_name", table_name="plugin_verified_signers")
    op.drop_table("plugin_verified_signers")

    for ix in ("ix_plugin_rating_user", "ix_plugin_rating_slug"):
        op.drop_index(ix, table_name="plugin_ratings")
    op.drop_table("plugin_ratings")
