"""Phase 7 / Module AK — Edge computing tables.

Adds four tables:
    edge_regions
    edge_nodes
    edge_routes
    edge_region_policies

Revision ID: helen_edge_addon
Revises: helen_federation_v2_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_edge_addon"
down_revision = "helen_federation_v2_addon"
branch_labels = ("helen_edge_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edge_regions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("data_residency_required", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("gdpr_compliant", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("latency_zone", sa.String(length=16), nullable=False,
                  server_default="warm"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_edge_regions_code"),
    )
    op.create_index("ix_edge_regions_country", "edge_regions", ["country"])

    op.create_table(
        "edge_nodes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("country", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("datacenter", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("advertise_url", sa.String(length=512), nullable=False),
        sa.Column("public_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("geo_lat", sa.Float(), nullable=False, server_default="0"),
        sa.Column("geo_lng", sa.Float(), nullable=False, server_default="0"),
        sa.Column("capacity", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("current_load_percent", sa.Float(), nullable=False,
                  server_default="0"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("node_id", name="uq_edge_nodes_node_id"),
    )
    op.create_index("ix_edge_nodes_region", "edge_nodes", ["region"])
    op.create_index("ix_edge_nodes_status", "edge_nodes", ["status"])
    op.create_index("ix_edge_nodes_heartbeat", "edge_nodes", ["last_heartbeat"])

    op.create_table(
        "edge_routes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True),
        sa.Column("edge_node_id", sa.String(length=32),
                  sa.ForeignKey("edge_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("current_load_percent", sa.Float(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("source_workspace_id", "edge_node_id",
                            name="uq_edge_routes_ws_node"),
    )
    op.create_index("ix_edge_routes_workspace", "edge_routes", ["source_workspace_id"])
    op.create_index("ix_edge_routes_node", "edge_routes", ["edge_node_id"])

    op.create_table(
        "edge_region_policies",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("allowed_regions", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("required_residency_region", sa.String(length=64), nullable=True),
        sa.Column("encryption_at_rest_required", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("audit_log_required", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", name="uq_edge_policy_workspace"),
    )


def downgrade() -> None:
    op.drop_table("edge_region_policies")
    op.drop_index("ix_edge_routes_node", table_name="edge_routes")
    op.drop_index("ix_edge_routes_workspace", table_name="edge_routes")
    op.drop_table("edge_routes")
    for ix in ("ix_edge_nodes_heartbeat", "ix_edge_nodes_status", "ix_edge_nodes_region"):
        op.drop_index(ix, table_name="edge_nodes")
    op.drop_table("edge_nodes")
    op.drop_index("ix_edge_regions_country", table_name="edge_regions")
    op.drop_table("edge_regions")
