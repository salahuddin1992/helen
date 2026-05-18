"""Phase 7 / Module AJ — Federation v2 mesh tables.

Adds five tables:
    federation_v2_servers
    federation_v2_users
    federation_v2_channels
    federation_v2_events
    federation_v2_trust_tokens

Revision ID: helen_federation_v2_addon
Revises: helen_security_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_federation_v2_addon"
down_revision = "helen_security_addon"
branch_labels = ("helen_federation_v2_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "federation_v2_servers",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("server_id", sa.String(length=255), nullable=False),
        sa.Column("public_key", sa.String(length=512), nullable=False),
        sa.Column("advertise_url", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("trust_level", sa.String(length=16), nullable=False,
                  server_default="peer"),
        sa.Column("trust_score", sa.Float(), nullable=False,
                  server_default="0.5"),
        sa.Column("version", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("signing_algo", sa.String(length=32), nullable=False,
                  server_default="ed25519"),
        sa.Column("capabilities", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("suspended_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("server_id", name="uq_fedv2_servers_server_id"),
    )
    op.create_index("ix_fedv2_servers_status", "federation_v2_servers", ["status"])
    op.create_index("ix_fedv2_servers_trust", "federation_v2_servers", ["trust_level"])
    op.create_index("ix_fedv2_servers_last_seen", "federation_v2_servers", ["last_seen"])

    op.create_table(
        "federation_v2_users",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("local_user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("remote_address", sa.String(length=320), nullable=False),
        sa.Column("origin_server", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False,
                  server_default=""),
        sa.Column("avatar_url", sa.String(length=512), nullable=True),
        sa.Column("public_key", sa.String(length=512), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("remote_address", name="uq_fedv2_users_addr"),
    )
    op.create_index("ix_fedv2_users_local_user", "federation_v2_users", ["local_user_id"])
    op.create_index("ix_fedv2_users_server", "federation_v2_users", ["origin_server"])
    op.create_index("ix_fedv2_users_last_seen", "federation_v2_users", ["last_seen"])

    op.create_table(
        "federation_v2_channels",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("federation_address", sa.String(length=320), nullable=False),
        sa.Column("origin_server", sa.String(length=255), nullable=False),
        sa.Column("shared_with", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("policy", sa.String(length=16), nullable=False,
                  server_default="public"),
        sa.Column("state_version", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("federation_address", name="uq_fedv2_channels_addr"),
    )
    op.create_index("ix_fedv2_channels_channel", "federation_v2_channels", ["channel_id"])
    op.create_index("ix_fedv2_channels_policy", "federation_v2_channels", ["policy"])

    op.create_table(
        "federation_v2_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("origin_server", sa.String(length=255), nullable=False),
        sa.Column("origin_event_id", sa.String(length=128), nullable=False),
        sa.Column("channel_address", sa.String(length=320), nullable=True),
        sa.Column("sender_address", sa.String(length=320), nullable=True),
        sa.Column("signed_payload", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("dag_parents", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("depth", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("processed", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("origin_server", "origin_event_id",
                            name="uq_fedv2_events_origin"),
    )
    op.create_index("ix_fedv2_events_kind", "federation_v2_events", ["kind"])
    op.create_index("ix_fedv2_events_channel", "federation_v2_events", ["channel_address"])
    op.create_index("ix_fedv2_events_processed", "federation_v2_events", ["processed"])
    op.create_index("ix_fedv2_events_depth", "federation_v2_events", ["depth"])
    op.create_index("ix_fedv2_events_origin_server", "federation_v2_events",
                    ["origin_server"])

    op.create_table(
        "federation_v2_trust_tokens",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("issuing_server", sa.String(length=255), nullable=False),
        sa.Column("subject_server", sa.String(length=255), nullable=False),
        sa.Column("signed_token", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False,
                  server_default="peer"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_fedv2_tokens_issuer",
                    "federation_v2_trust_tokens", ["issuing_server"])
    op.create_index("ix_fedv2_tokens_subject",
                    "federation_v2_trust_tokens", ["subject_server"])
    op.create_index("ix_fedv2_tokens_expires",
                    "federation_v2_trust_tokens", ["expires_at"])


def downgrade() -> None:
    for ix in ("ix_fedv2_tokens_expires",
               "ix_fedv2_tokens_subject",
               "ix_fedv2_tokens_issuer"):
        op.drop_index(ix, table_name="federation_v2_trust_tokens")
    op.drop_table("federation_v2_trust_tokens")

    for ix in ("ix_fedv2_events_origin_server", "ix_fedv2_events_depth",
               "ix_fedv2_events_processed", "ix_fedv2_events_channel",
               "ix_fedv2_events_kind"):
        op.drop_index(ix, table_name="federation_v2_events")
    op.drop_table("federation_v2_events")

    for ix in ("ix_fedv2_channels_policy", "ix_fedv2_channels_channel"):
        op.drop_index(ix, table_name="federation_v2_channels")
    op.drop_table("federation_v2_channels")

    for ix in ("ix_fedv2_users_last_seen", "ix_fedv2_users_server",
               "ix_fedv2_users_local_user"):
        op.drop_index(ix, table_name="federation_v2_users")
    op.drop_table("federation_v2_users")

    for ix in ("ix_fedv2_servers_last_seen", "ix_fedv2_servers_trust",
               "ix_fedv2_servers_status"):
        op.drop_index(ix, table_name="federation_v2_servers")
    op.drop_table("federation_v2_servers")
