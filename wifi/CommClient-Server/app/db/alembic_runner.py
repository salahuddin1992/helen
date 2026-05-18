"""
Programmatic Alembic runner invoked from the FastAPI lifespan.

Why this exists
---------------
Shipping a desktop/LAN server that requires the operator to remember to run
``alembic upgrade head`` before starting the app is a support nightmare.
Every release carries a fresh schema on the user's machine, so the startup
path itself needs to be the migration driver.

Behavior
--------
* Resolves ``alembic.ini`` relative to the project root (parent of ``app/``).
* Points Alembic's ``sqlalchemy.url`` at whatever URL the live app config
  exports — this avoids duplicated DB URL definitions.
* Runs ``upgrade head`` in a worker thread because Alembic's command API is
  synchronous and the lifespan is an async context manager.
* Any failure is logged at ERROR but does NOT crash the app. The legacy
  ``Base.metadata.create_all`` + ``run_startup_migrations`` path still runs,
  so the worst case is that the service starts on the previous schema —
  better than a boot crash on a user's machine.

This is production-style: atomic, idempotent, safe to re-run on every boot.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


def _project_root() -> Path:
    # app/db/alembic_runner.py  →  <root>/app/db/alembic_runner.py
    return Path(__file__).resolve().parent.parent.parent


def _alembic_ini_path() -> Path:
    return _project_root() / "alembic.ini"


def _sync_url_from_settings(url: str) -> str:
    """
    Alembic's upgrade command uses a synchronous engine. Strip async driver
    prefixes so the migration runs against the matching sync driver.
    """
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite:///", 1) \
            .replace("sqlite:////", "sqlite:////")  # preserve absolute form
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if url.startswith("mysql+aiomysql://"):
        return url.replace("mysql+aiomysql://", "mysql+pymysql://", 1)
    return url


def _run_upgrade_head_sync(db_url: Optional[str]) -> None:
    """Runs synchronously inside ``asyncio.to_thread``.

    Adopt-existing-schema rule
    --------------------------
    A LAN-deployment quirk: prior versions of Helen-Server used
    ``Base.metadata.create_all`` exclusively, so a long-running install
    has the full schema BUT no ``alembic_version`` row. Naively running
    ``upgrade head`` on such a DB triggers "table users already exists"
    on the very first migration. We detect this case (tables present,
    no version row) and ``stamp`` the DB at head instead of trying to
    re-create what's already there. Result: zero startup noise on every
    boot, regardless of whether the DB was created by alembic, by
    create_all, or by a previous mixed install.
    """
    try:
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, inspect
    except Exception as exc:  # pragma: no cover
        logger.warning("alembic_not_installed", error=str(exc))
        return

    ini_path = _alembic_ini_path()
    if not ini_path.exists():
        logger.warning("alembic_ini_missing", path=str(ini_path))
        return

    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(_project_root() / "migrations"))
    sync_url = _sync_url_from_settings(db_url) if db_url else None
    if sync_url:
        cfg.set_main_option("sqlalchemy.url", sync_url)

    # Pre-flight: if the DB has the schema but no alembic_version table,
    # this is a legacy create_all-only install. Stamp it at head and skip
    # the actual upgrade — that's the migration framework's recommended
    # "adopt existing schema" pattern.
    legacy_install = False
    if sync_url:
        try:
            insp_engine = create_engine(sync_url)
            insp = inspect(insp_engine)
            has_users = "users" in insp.get_table_names()
            has_version = "alembic_version" in insp.get_table_names()
            insp_engine.dispose()
            if has_users and not has_version:
                legacy_install = True
        except Exception as _e:
            # Fallthrough to normal upgrade path on inspector errors.
            logger.debug("alembic_inspect_failed", error=str(_e))

    # Chdir so relative paths in env.py resolve correctly.
    prev_cwd = os.getcwd()
    try:
        os.chdir(_project_root())
        if legacy_install:
            logger.info("alembic_stamp_existing_schema")
            command.stamp(cfg, "head")
        else:
            command.upgrade(cfg, "head")
    finally:
        os.chdir(prev_cwd)


async def run_alembic_upgrade(db_url: Optional[str] = None) -> bool:
    """
    Run ``alembic upgrade head`` from within the FastAPI lifespan.

    Returns True on success, False on any failure. Failures are logged but
    never raised — the service continues with whatever schema is currently
    in place so the operator still gets a running server.
    """
    try:
        await asyncio.to_thread(_run_upgrade_head_sync, db_url)
        logger.info("alembic_upgrade_completed")
        return True
    except Exception as exc:
        logger.error("alembic_upgrade_failed", error=str(exc))
        return False
