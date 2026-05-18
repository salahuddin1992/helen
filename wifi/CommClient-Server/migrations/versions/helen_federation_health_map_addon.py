"""Federation Health Map addon — operator tables for the admin panel.

Adds five tables:

    federation_peer_meta       — operator-facing per-peer metadata
    federation_shaper_rules    — token-bucket bandwidth rules
    federation_policies        — routing policies
    federation_certs           — per-peer mTLS certs + lifecycle
    federation_event_log       — operational event timeline

Revision ID: helen_federation_health_map_addon
Revises: helen_tenancy_portal_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_federation_health_map_addon"
down_revision = "helen_tenancy_portal_addon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── federation_peer_meta ────────────────────────────────────
    op.create_table(
        "federation_peer_meta",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("server_id", sa.String(length=255), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False,
                  server_default=""),
        sa.Column("ip_address", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("region", sa.String(length=64), nullable=False,
                  server_default="default"),
        sa.Column("role", sa.String(length=16), nullable=False,
                  server_default="follower"),
        sa.Column("health_state", sa.String(length=16), nullable=False,
                  server_default="unknown"),
        sa.Column("quarantined", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("quarantined_reason", sa.Text(), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shaper_rule_id", sa.String(length=32), nullable=True),
        sa.Column("cert_id", sa.String(length=32), nullable=True),
        sa.Column("last_handshake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rtt_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_throughput_kbps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_loss_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("server_id", name="uq_fed_peer_meta_server"),
    )
    op.create_index("ix_fed_peer_meta_role", "federation_peer_meta", ["role"])
    op.create_index("ix_fed_peer_meta_region", "federation_peer_meta", ["region"])
    op.create_index("ix_fed_peer_meta_quarantined",
                    "federation_peer_meta", ["quarantined"])

    # ── federation_shaper_rules ─────────────────────────────────
    op.create_table(
        "federation_shaper_rules",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("server_id", sa.String(length=255), nullable=False),
        sa.Column("preset", sa.String(length=16), nullable=False,
                  server_default="custom"),
        sa.Column("in_kbps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("out_kbps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("burst_kbps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("active", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("params", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False,
                  server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_fed_shaper_server", "federation_shaper_rules", ["server_id"])
    op.create_index("ix_fed_shaper_active", "federation_shaper_rules", ["active"])

    # ── federation_policies ─────────────────────────────────────
    op.create_table(
        "federation_policies",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("match", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("action", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.String(length=64), nullable=False,
                  server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_fed_policy_priority", "federation_policies", ["priority"])
    op.create_index("ix_fed_policy_enabled", "federation_policies", ["enabled"])

    # ── federation_certs ────────────────────────────────────────
    op.create_table(
        "federation_certs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("server_id", sa.String(length=255), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=128), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("issuer", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("serial", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chain_depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("chain_pem", sa.Text(), nullable=True),
        sa.Column("leaf_pem", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("rotation_reason", sa.Text(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_fed_cert_server", "federation_certs", ["server_id"])
    op.create_index("ix_fed_cert_active", "federation_certs", ["active"])
    op.create_index("ix_fed_cert_expires", "federation_certs", ["not_after"])

    # ── federation_event_log ────────────────────────────────────
    op.create_table(
        "federation_event_log",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("server_id", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False,
                  server_default="info"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("success", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_fed_event_server", "federation_event_log", ["server_id"])
    op.create_index("ix_fed_event_category", "federation_event_log", ["category"])
    op.create_index("ix_fed_event_occurred", "federation_event_log", ["occurred_at"])
    op.create_index("ix_fed_event_severity", "federation_event_log", ["severity"])


def downgrade() -> None:
    op.drop_index("ix_fed_event_severity", table_name="federation_event_log")
    op.drop_index("ix_fed_event_occurred", table_name="federation_event_log")
    op.drop_index("ix_fed_event_category", table_name="federation_event_log")
    op.drop_index("ix_fed_event_server", table_name="federation_event_log")
    op.drop_table("federation_event_log")

    op.drop_index("ix_fed_cert_expires", table_name="federation_certs")
    op.drop_index("ix_fed_cert_active", table_name="federation_certs")
    op.drop_index("ix_fed_cert_server", table_name="federation_certs")
    op.drop_table("federation_certs")

    op.drop_index("ix_fed_policy_enabled", table_name="federation_policies")
    op.drop_index("ix_fed_policy_priority", table_name="federation_policies")
    op.drop_table("federation_policies")

    op.drop_index("ix_fed_shaper_active", table_name="federation_shaper_rules")
    op.drop_index("ix_fed_shaper_server", table_name="federation_shaper_rules")
    op.drop_table("federation_shaper_rules")

    op.drop_index("ix_fed_peer_meta_quarantined", table_name="federation_peer_meta")
    op.drop_index("ix_fed_peer_meta_region", table_name="federation_peer_meta")
    op.drop_index("ix_fed_peer_meta_role", table_name="federation_peer_meta")
    op.drop_table("federation_peer_meta")
