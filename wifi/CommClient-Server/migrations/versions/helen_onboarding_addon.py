"""Operator Onboarding Wizard — persistence tables.

Adds four tables backing the 14-step wizard:

    onboarding_state         — single-row wizard cursor + draft data
    system_certs             — operator-managed TLS certs (server/root/operator)
    router_pairings          — TOFU-confirmed router public keys
    admin_recovery_codes     — one-time recovery codes (hashed)

Revision ID: helen_onboarding_addon
Revises: helen_dr_v2_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_onboarding_addon"
down_revision = "helen_dr_v2_addon"
branch_labels = None
# Absorb the two pre-existing dangling heads from older branches so that
# `alembic heads` returns exactly one head after this migration applies.
depends_on = None


def upgrade() -> None:
    # ── onboarding_state ─────────────────────────────────────
    op.create_table(
        "onboarding_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("completed_steps", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("current_step", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("draft_data", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    # ── system_certs ─────────────────────────────────────────
    op.create_table(
        "system_certs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("role", sa.String(length=32), nullable=False,
                  server_default="server"),
        sa.Column("key_type", sa.String(length=16), nullable=False,
                  server_default="rsa"),
        sa.Column("common_name", sa.String(length=255), nullable=False),
        sa.Column("san_list", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("fingerprint_sha256", sa.String(length=95), nullable=False),
        sa.Column("serial_number", sa.String(length=64), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cert_pem", sa.Text(), nullable=False),
        sa.Column("key_pem_encrypted", sa.Text(), nullable=True),
        sa.Column("is_self_signed", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("active", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("extra", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_system_certs_role", "system_certs", ["role"])
    op.create_index("ix_system_certs_fingerprint",
                    "system_certs", ["fingerprint_sha256"])
    op.create_index("ix_system_certs_not_after", "system_certs", ["not_after"])
    op.create_index("ix_system_certs_active", "system_certs", ["active"])

    # ── router_pairings ──────────────────────────────────────
    op.create_table(
        "router_pairings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("router_url", sa.String(length=512), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=95), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ping_rtt_ms", sa.Integer(), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_router_pairings_url", "router_pairings", ["router_url"])
    op.create_index("ix_router_pairings_fingerprint",
                    "router_pairings", ["fingerprint_sha256"])
    op.create_index("ix_router_pairings_status", "router_pairings", ["status"])

    # ── admin_recovery_codes ─────────────────────────────────
    op.create_table(
        "admin_recovery_codes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("code_hash", sa.String(length=128),
                  nullable=False, unique=True),
        sa.Column("used", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_recovery_codes_user",
                    "admin_recovery_codes", ["user_id"])
    op.create_index("ix_admin_recovery_codes_used",
                    "admin_recovery_codes", ["used"])


def downgrade() -> None:
    for ix in ("ix_admin_recovery_codes_used",
               "ix_admin_recovery_codes_user"):
        op.drop_index(ix, table_name="admin_recovery_codes")
    op.drop_table("admin_recovery_codes")

    for ix in ("ix_router_pairings_status",
               "ix_router_pairings_fingerprint",
               "ix_router_pairings_url"):
        op.drop_index(ix, table_name="router_pairings")
    op.drop_table("router_pairings")

    for ix in ("ix_system_certs_active",
               "ix_system_certs_not_after",
               "ix_system_certs_fingerprint",
               "ix_system_certs_role"):
        op.drop_index(ix, table_name="system_certs")
    op.drop_table("system_certs")

    op.drop_table("onboarding_state")
