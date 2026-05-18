"""user share_code

Revision ID: 009
Revises: 008
Create Date: 2026-04-20

Adds the 64-char public share_code column users hand out so peers can find
them without knowing their UUID or username. Backfills existing rows with
a unique code before applying the NOT NULL + UNIQUE constraints.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def _generate_code() -> str:
    import secrets
    alphabet = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
    )
    return "".join(secrets.choice(alphabet) for _ in range(64))


def upgrade() -> None:
    # Step 1: add as nullable so existing rows don't break the ALTER.
    op.add_column("users", sa.Column("share_code", sa.String(64), nullable=True))

    # Step 2: backfill every pre-existing row with a fresh unique code.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id FROM users WHERE share_code IS NULL")
    ).fetchall()
    used: set[str] = set()
    for row in rows:
        for _ in range(8):
            code = _generate_code()
            if code in used:
                continue
            dup = bind.execute(
                sa.text("SELECT 1 FROM users WHERE share_code = :c"),
                {"c": code},
            ).first()
            if dup is None:
                break
        used.add(code)
        bind.execute(
            sa.text("UPDATE users SET share_code = :c WHERE id = :uid"),
            {"c": code, "uid": row[0]},
        )

    # Step 3: enforce NOT NULL now that every row has a value.
    # SQLite can't ALTER a column's nullability, so we skip on SQLite and
    # rely on the unique index + application-level NOT NULL guarantee.
    if bind.dialect.name != "sqlite":
        op.alter_column("users", "share_code", nullable=False)

    # Step 4: unique index for lookup + collision prevention.
    op.create_index(
        "ix_users_share_code_unique",
        "users",
        ["share_code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_share_code_unique", table_name="users")
    op.drop_column("users", "share_code")
