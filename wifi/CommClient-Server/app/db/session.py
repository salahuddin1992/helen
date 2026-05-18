"""
Async SQLAlchemy engine and session factory.
Supports SQLite (dev/small deploy) and PostgreSQL (production scale).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import get_settings

settings = get_settings()

# Engine kwargs differ between SQLite and PostgreSQL
_engine_kwargs: dict = {}
if settings.DB_BACKEND == "sqlite":
    # For in-memory sqlite we MUST share a single connection (StaticPool) so
    # the memory-resident DB survives across sessions. For file-backed sqlite
    # NullPool is much safer under concurrent load: each session opens its own
    # connection, avoiding the "Could not refresh instance" race that happens
    # when StaticPool multiplexes async writers over a single raw connection.
    _engine_kwargs = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool if ":memory:" in settings.db_url else NullPool,
    }
else:
    _engine_kwargs = {
        "pool_size": 20,
        "max_overflow": 10,
        "pool_pre_ping": True,
    }

engine = create_async_engine(
    settings.db_url,
    echo=settings.DEBUG,
    **_engine_kwargs,
)

# ── SQLite production hardening ────────────────────────────────────────────
# Install WAL + concurrency pragmas on every new physical connection. Safe on
# PostgreSQL (no-op unless the underlying driver is SQLite) and idempotent.
if settings.DB_BACKEND == "sqlite":
    try:
        from app.db.sqlite_tuning import install_sqlite_pragmas
        install_sqlite_pragmas(engine)
    except Exception:  # pragma: no cover — never block startup on tuning
        import logging
        logging.getLogger(__name__).exception("sqlite_tuning_install_failed")

    # ── At-rest DB encryption (opt-in via HELEN_DB_ENCRYPTED=1) ───────────
    # When enabled, every new SQLite connection runs ``PRAGMA key=…`` so the
    # underlying file is AES-encrypted. Requires ``pysqlcipher3`` to be
    # installed AND the running aiosqlite-equivalent driver to be sqlcipher-
    # aware. If the prerequisites aren't met the listener logs an explicit
    # warning and continues (falls back to plain SQLite). Key is loaded
    # from ``$DATA_DIR/db-master.key`` (created on first boot).
    import os as _os_dbenc
    if _os_dbenc.environ.get("HELEN_DB_ENCRYPTED", "").lower() in ("1", "true", "yes"):
        import logging as _log_dbenc
        _dbenc_logger = _log_dbenc.getLogger("app.db.encryption")
        try:
            from sqlalchemy import event as _sa_event
            from app.services.db_encryption import (
                load_or_create_db_master_key,
                extract_key,
                is_native_encryption_available,
            )
            from pathlib import Path as _DBP
            _data_dir = _DBP(settings.SQLITE_PATH)
            _data_dir = (_data_dir.resolve().parent if _data_dir.is_absolute()
                         else (settings.PROJECT_ROOT / _data_dir).resolve().parent)
            _data_dir.mkdir(parents=True, exist_ok=True)
            _passphrase = _os_dbenc.environ.get("HELEN_DB_MASTER_KEY") or None
            _master_blob = load_or_create_db_master_key(str(_data_dir),
                                                        passphrase=_passphrase)
            _master_key = extract_key(_master_blob)
            _hex_key = _master_key.hex()

            if not is_native_encryption_available():
                _dbenc_logger.warning(
                    "db_encryption_native_unavailable",
                    detail="pysqlcipher3 not importable; HELEN_DB_ENCRYPTED is "
                           "set but the running build can't apply PRAGMA key. "
                           "Install pysqlcipher3 and rebuild Helen-Server, "
                           "OR fall back to field-level encryption for "
                           "individual columns. Continuing with PLAIN SQLite.",
                )
            else:
                @_sa_event.listens_for(engine.sync_engine, "connect")
                def _apply_sqlcipher_key(dbapi_conn, _conn_record):
                    try:
                        cur = dbapi_conn.cursor()
                        cur.execute(f"PRAGMA key = \"x'{_hex_key}'\"")
                        cur.execute("PRAGMA cipher_page_size = 4096")
                        cur.execute("PRAGMA kdf_iter = 256000")
                        cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
                        cur.execute("PRAGMA cipher_kdf_algorithm = "
                                    "PBKDF2_HMAC_SHA512")
                        cur.close()
                    except Exception as _exc:
                        _dbenc_logger.error(
                            "sqlcipher_key_apply_failed",
                            error=str(_exc),
                        )
                _dbenc_logger.info("db_encryption_listener_installed",
                                   master_key_path=str(_data_dir / "db-master.key"))
        except Exception as _exc:
            _dbenc_logger.error("db_encryption_setup_failed", error=str(_exc))

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
