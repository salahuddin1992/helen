"""
Lightweight startup column-add migrations.

Runs after `Base.metadata.create_all` to add NEW columns to PRE-EXISTING
tables on databases that already shipped before the column existed.

This is intentionally minimal — for full schema rewrites, use Alembic.
Each migration is idempotent: it inspects the live schema first, only
applies the ALTER if the column is missing, and never drops/changes
existing data.

Add new column entries to `_PENDING_COLUMN_ADDS` below as the schema grows.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger

logger = get_logger(__name__)


# (table, column, sql_type) — sql_type is portable enough for both SQLite & Postgres
_PENDING_COLUMN_ADDS: list[tuple[str, str, str]] = [
    # Custom user status messages (task #52)
    ("users", "status_message", "VARCHAR(140)"),
    ("users", "status_expires_at", "TIMESTAMP"),
    # RBAC role ("user" | "moderator" | "admin") — added post-initial migration.
    # create_all skips already-existing tables so fresh packaged builds need
    # this ALTER to pick up the column the ORM expects.
    ("users", "role", "VARCHAR(16) DEFAULT 'user' NOT NULL"),
    # Channel archive/mute/last-read (task #53)
    ("channel_members", "last_read_message_id", "VARCHAR(32)"),
    ("channel_members", "mute_until", "TIMESTAMP"),
    ("channel_members", "is_archived", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("channel_members", "is_pinned", "BOOLEAN DEFAULT 0 NOT NULL"),
    # Group ban (audit fix 1.4) — separate from full DM block. A banned
    # member stays in the channel record (so receipts/back-history
    # don't break) but cannot send.
    ("channel_members", "banned_at", "TIMESTAMP"),
    ("channel_members", "banned_until", "TIMESTAMP"),
    ("channel_members", "banned_by", "VARCHAR(32)"),
    # Message idempotency (audit fix 1.3). NOTE: SQLite cannot ADD a
    # UNIQUE column inline; we add the column unconstrained here and
    # the ORM-level UniqueConstraint takes effect on fresh databases.
    # Existing dbs are protected by the application-level dedup check
    # in MessageService.send_message.
    ("messages", "client_message_id", "VARCHAR(64)"),
    # Federation: which server holds the in-memory ActiveCall (BLOCKER-2).
    ("active_calls", "origin_server_id", "VARCHAR(128)"),
    # H-2 per-chunk integrity for group file offers (NULL = opted out).
    ("group_file_offers", "chunk_hashes_json", "TEXT"),
]


_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Strict allowlist for SQL identifiers we splice into PRAGMA
    queries. PRAGMA can't accept bind parameters for table names,
    so we have to interpolate — but only after verifying the input
    is a plain identifier (no quotes, semicolons, parentheses,
    spaces). Raises ValueError if the input is even slightly weird."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


async def _column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table. Works for SQLite + Postgres."""
    dialect = conn.dialect.name
    if dialect == "sqlite":
        # PRAGMA table_info() doesn't accept bind parameters for the
        # table name, so we *have* to interpolate. Sanitise via
        # _safe_ident first to keep the f-string safe even though
        # callers come from a hardcoded migration list right now.
        safe_table = _safe_ident(table)
        result = await conn.execute(text(f"PRAGMA table_info({safe_table})"))
        rows = result.fetchall()
        return any(row[1] == column for row in rows)
    else:
        # Postgres / others — use information_schema
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        return result.first() is not None


async def _table_exists(conn, table: str) -> bool:
    dialect = conn.dialect.name
    if dialect == "sqlite":
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )
        return result.first() is not None
    else:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
            ),
            {"t": table},
        )
        return result.first() is not None


def _sa_type_to_sql(col_type, dialect_name: str) -> str:
    """Render a SQLAlchemy column type to DDL appropriate for the dialect."""
    try:
        from sqlalchemy.schema import CreateColumn  # noqa: F401
        # Compile with the right dialect so VARCHAR(n), DATETIME, BOOLEAN, etc.
        # come out right on both SQLite and Postgres.
        if dialect_name == "sqlite":
            from sqlalchemy.dialects import sqlite as _d
            return col_type.compile(dialect=_d.dialect())
        else:
            from sqlalchemy.dialects import postgresql as _d
            return col_type.compile(dialect=_d.dialect())
    except Exception:
        return str(col_type)


async def _align_model_columns(engine: AsyncEngine) -> tuple[int, int]:
    """
    Walk every ORM-mapped table in Base.metadata and ADD any columns the
    DB is missing. This is a safety net for databases created by an older
    Alembic revision that doesn't yet know about newer model attributes.

    Idempotent: columns that already exist are skipped. Columns with
    server-side defaults fill automatically on the ALTER; columns without a
    default become NULL for old rows, which matches the SQLAlchemy default.

    Returns (added, skipped).
    """
    from app.db.base import Base
    # Make sure every model class has registered with Base.metadata. Some
    # tables are only imported through service modules — force-load them.
    import app.models  # noqa: F401

    added = 0
    skipped = 0
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        for table_name, table in Base.metadata.tables.items():
            if not await _table_exists(conn, table_name):
                # create_all (or alembic) hasn't created it yet — leave alone.
                continue
            for column in table.columns:
                if await _column_exists(conn, table_name, column.name):
                    skipped += 1
                    continue
                # Render the type portably per dialect.
                type_sql = _sa_type_to_sql(column.type, dialect)
                pieces = [type_sql]
                # server_default gives old rows a value so we can keep NOT NULL
                if column.server_default is not None:
                    default_sql = column.server_default.arg
                    if hasattr(default_sql, "text"):
                        default_sql = default_sql.text
                    pieces.append(f"DEFAULT {default_sql}")
                elif not column.nullable:
                    # SQLite can't add a NOT NULL column without a default.
                    # Best effort: emit nullable and log a warning.
                    logger.warning(
                        "startup_migration_not_null_without_default",
                        table=table_name,
                        column=column.name,
                        note="adding as NULLABLE on an existing table",
                    )
                if column.nullable is False and column.server_default is not None:
                    pieces.append("NOT NULL")
                ddl = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {' '.join(pieces)}"
                try:
                    await conn.execute(text(ddl))
                    logger.info(
                        "startup_migration_column_added",
                        table=table_name,
                        column=column.name,
                        ddl=ddl,
                    )
                    added += 1
                except Exception as e:
                    logger.warning(
                        "startup_migration_column_add_failed",
                        table=table_name,
                        column=column.name,
                        ddl=ddl,
                        error=str(e),
                    )
    return added, skipped


async def _backfill_share_codes(engine: AsyncEngine) -> int:
    """Populate users.share_code on rows that pre-date the column.

    The generic aligner adds the column as NULLABLE (SQLite can't add a
    NOT NULL column without a default). We fill every NULL row with a
    fresh 64-char code and then create the UNIQUE INDEX so future inserts
    are protected against collision.

    Returns the number of rows that were backfilled.
    """
    from app.core.share_code import generate_share_code

    filled = 0
    async with engine.begin() as conn:
        if not await _table_exists(conn, "users"):
            return 0
        if not await _column_exists(conn, "users", "share_code"):
            return 0

        # Pull every row that is still missing a code.
        result = await conn.execute(
            text("SELECT id FROM users WHERE share_code IS NULL OR share_code = ''")
        )
        rows = result.fetchall()

        used: set[str] = set()
        for row in rows:
            # Re-draw on the off chance of a collision against another
            # freshly-minted code in this same batch.
            for _ in range(8):
                code = generate_share_code()
                if code in used:
                    continue
                dup = await conn.execute(
                    text("SELECT 1 FROM users WHERE share_code = :c"),
                    {"c": code},
                )
                if dup.first() is None:
                    break
            used.add(code)
            await conn.execute(
                text("UPDATE users SET share_code = :c WHERE id = :uid"),
                {"c": code, "uid": row[0]},
            )
            filled += 1

        # Ensure the uniqueness guarantee is enforced at the DB level.
        # CREATE UNIQUE INDEX IF NOT EXISTS is portable across SQLite and
        # Postgres and cheap to re-run on every startup.
        try:
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_users_share_code_unique ON users (share_code)"
                )
            )
        except Exception as e:
            logger.warning("share_code_unique_index_failed", error=str(e))

    if filled:
        logger.info("share_code_backfill_done", rows_filled=filled)
    return filled


async def run_startup_migrations(engine: AsyncEngine) -> None:
    """Apply all pending lightweight column-add migrations."""
    applied = 0
    skipped = 0
    async with engine.begin() as conn:
        for table, column, sql_type in _PENDING_COLUMN_ADDS:
            try:
                if not await _table_exists(conn, table):
                    # create_all will handle this — nothing to migrate
                    skipped += 1
                    continue
                if await _column_exists(conn, table, column):
                    skipped += 1
                    continue
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                )
                logger.info(
                    "startup_migration_applied",
                    table=table,
                    column=column,
                    type=sql_type,
                )
                applied += 1
            except Exception as e:
                # Never fail startup over a column add — log and continue
                logger.warning(
                    "startup_migration_failed",
                    table=table,
                    column=column,
                    error=str(e),
                )

    # Generic schema alignment: catches every column we forgot to list above.
    try:
        g_added, g_skipped = await _align_model_columns(engine)
        logger.info(
            "startup_migrations_model_align_done",
            added=g_added,
            skipped=g_skipped,
        )
        applied += g_added
        skipped += g_skipped
    except Exception as e:
        logger.warning("startup_migrations_model_align_failed", error=str(e))

    # Post-column backfills — must run AFTER the column physically exists.
    try:
        await _backfill_share_codes(engine)
    except Exception as e:
        logger.warning("share_code_backfill_failed", error=str(e))

    # Seed builtin camera quality presets (4K/8K/1080p/etc.) on first boot.
    # Idempotent — only inserts presets whose stable id isn't already in the
    # table, so admin edits to builtin rows survive across restarts.
    try:
        await _seed_camera_presets(engine)
    except Exception as e:
        logger.warning("camera_preset_seed_failed", error=str(e))

    logger.info("startup_migrations_done", applied=applied, skipped=skipped)


async def _seed_camera_presets(engine: AsyncEngine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services.media_policy_service import media_policy_service

    async with engine.begin() as conn:
        if not await _table_exists(conn, "camera_quality_presets"):
            return

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        try:
            inserted = await media_policy_service.seed_builtin_presets(session)
            await session.commit()
            if inserted:
                logger.info("camera_presets_seed_done", inserted=inserted)
        except Exception:
            await session.rollback()
            raise
