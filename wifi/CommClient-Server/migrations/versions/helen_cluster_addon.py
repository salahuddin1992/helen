"""Phase 6 / Module AC — Cluster tables.

Adds two tables:
    cluster_nodes
    cluster_leader_elect

Revision ID: helen_cluster_addon
Revises: helen_ai_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_cluster_addon"
down_revision = "helen_ai_addon"
branch_labels = ("helen_cluster_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cluster_nodes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("advertise_url", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="joining"),
        sa.Column("role", sa.String(length=16), nullable=False,
                  server_default="replica"),
        sa.Column("version", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("joined_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("capabilities", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("node_id", name="uq_cluster_nodes_node_id"),
    )
    op.create_index("ix_cluster_nodes_status",
                    "cluster_nodes", ["status"])
    op.create_index("ix_cluster_nodes_last_seen",
                    "cluster_nodes", ["last_seen"])

    op.create_table(
        "cluster_leader_elect",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("term", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("leader_node_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lock_token", sa.String(length=128), nullable=False),
    )
    op.create_index("ix_cluster_leader_term",
                    "cluster_leader_elect", ["term"])
    op.create_index("ix_cluster_leader_expires",
                    "cluster_leader_elect", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_cluster_leader_expires",
                  table_name="cluster_leader_elect")
    op.drop_index("ix_cluster_leader_term",
                  table_name="cluster_leader_elect")
    op.drop_table("cluster_leader_elect")

    op.drop_index("ix_cluster_nodes_last_seen", table_name="cluster_nodes")
    op.drop_index("ix_cluster_nodes_status", table_name="cluster_nodes")
    op.drop_table("cluster_nodes")
