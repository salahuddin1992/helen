"""Phase 6 / Module AE — Security tables.

Adds four tables:
    ip_blocks
    login_attempts
    security_events
    security_advisories

Revision ID: helen_security_addon
Revises: helen_cluster_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_security_addon"
down_revision = "helen_cluster_addon"
branch_labels = ("helen_security_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ip_blocks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("ip_cidr", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocked_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ip_blocks_cidr", "ip_blocks", ["ip_cidr"])
    op.create_index("ix_ip_blocks_expires", "ip_blocks", ["expires_at"])

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("attempted_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_login_attempts_username", "login_attempts", ["username"])
    op.create_index("ix_login_attempts_ip", "login_attempts", ["ip"])
    op.create_index("ix_login_attempts_attempted_at",
                    "login_attempts", ["attempted_at"])

    op.create_table(
        "security_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False,
                  server_default="info"),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_security_events_kind", "security_events", ["kind"])
    op.create_index("ix_security_events_severity", "security_events", ["severity"])
    op.create_index("ix_security_events_created_at",
                    "security_events", ["created_at"])
    op.create_index("ix_security_events_ip", "security_events", ["ip"])

    op.create_table(
        "security_advisories",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("package", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("cve", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False,
                  server_default="unknown"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("fixed_in", sa.String(length=128), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_security_advisories_package",
                    "security_advisories", ["package"])
    op.create_index("ix_security_advisories_severity",
                    "security_advisories", ["severity"])
    op.create_index("ix_security_advisories_acknowledged",
                    "security_advisories", ["acknowledged"])


def downgrade() -> None:
    for ix in ("ix_security_advisories_acknowledged",
               "ix_security_advisories_severity",
               "ix_security_advisories_package"):
        op.drop_index(ix, table_name="security_advisories")
    op.drop_table("security_advisories")

    for ix in ("ix_security_events_ip",
               "ix_security_events_created_at",
               "ix_security_events_severity",
               "ix_security_events_kind"):
        op.drop_index(ix, table_name="security_events")
    op.drop_table("security_events")

    for ix in ("ix_login_attempts_attempted_at",
               "ix_login_attempts_ip",
               "ix_login_attempts_username"):
        op.drop_index(ix, table_name="login_attempts")
    op.drop_table("login_attempts")

    op.drop_index("ix_ip_blocks_expires", table_name="ip_blocks")
    op.drop_index("ix_ip_blocks_cidr", table_name="ip_blocks")
    op.drop_table("ip_blocks")
