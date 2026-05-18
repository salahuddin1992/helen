#!/usr/bin/env python3
"""
Database migration helper script.

Provides convenient CLI for common Alembic operations:
  - python scripts/db_migrate.py status     # Show current migration state
  - python scripts/db_migrate.py upgrade    # Apply all pending migrations
  - python scripts/db_migrate.py downgrade  # Revert last migration
  - python scripts/db_migrate.py reset      # Reset database (dev only)
  - python scripts/db_migrate.py sql        # Generate SQL without applying
"""

from __future__ import annotations

import sys
import asyncio
import subprocess
from pathlib import Path
from typing import NoReturn

from app.core.config import get_settings
from app.db.session import engine
from app.db.base import Base


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def run_alembic(*args: str) -> int:
    """Run Alembic command and return exit code."""
    cmd = ["alembic", "-c", "alembic.ini"] + list(args)
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    return result.returncode


async def init_db() -> None:
    """Create all tables from model metadata (dev fallback)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Database initialized from model metadata")


async def drop_all() -> None:
    """Drop all tables (dev only, dangerous)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    print("⚠ All tables dropped")


def main() -> NoReturn:
    """Main entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1]
    settings = get_settings()

    if command == "status":
        """Show current migration state."""
        print(f"\nDatabase: {settings.db_url}")
        print(f"Backend:  {settings.DB_BACKEND}\n")
        run_alembic("current")
        print()
        exit_code = run_alembic("heads")

    elif command == "upgrade":
        """Upgrade to latest migration."""
        if len(sys.argv) > 2:
            target = sys.argv[2]
            exit_code = run_alembic("upgrade", target)
        else:
            exit_code = run_alembic("upgrade", "head")

    elif command == "downgrade":
        """Downgrade one migration (or to target)."""
        if len(sys.argv) > 2:
            target = sys.argv[2]
            exit_code = run_alembic("downgrade", target)
        else:
            exit_code = run_alembic("downgrade", "-1")

    elif command == "reset":
        """Reset database to initial state (dev only)."""
        confirm = input("⚠ Drop ALL tables and downgrade? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelled")
            sys.exit(0)

        exit_code = run_alembic("downgrade", "base")
        if exit_code == 0:
            exit_code = run_alembic("upgrade", "head")

    elif command == "sql":
        """Generate SQL for pending migrations without applying."""
        if len(sys.argv) > 2:
            target = sys.argv[2]
            exit_code = run_alembic("upgrade", target, "--sql")
        else:
            exit_code = run_alembic("upgrade", "head", "--sql")

    elif command == "history":
        """Show migration history."""
        exit_code = run_alembic("history")

    elif command == "current":
        """Show current migration."""
        exit_code = run_alembic("current")

    elif command == "new" or command == "revision":
        """Create new migration."""
        if len(sys.argv) < 3:
            print("Usage: db_migrate.py revision <message>")
            sys.exit(1)
        message = " ".join(sys.argv[2:])
        exit_code = run_alembic("revision", "--autogenerate", "-m", message)

    elif command == "init-fallback":
        """Initialize database from model metadata (fallback, no migration tracking)."""
        confirm = input("Create tables from model metadata? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelled")
            sys.exit(0)
        asyncio.run(init_db())
        sys.exit(0)

    elif command == "drop-all":
        """Drop all tables (dev only, very dangerous)."""
        confirm = (
            input("⚠⚠⚠ DROP ALL TABLES? This cannot be undone! (y/N): ")
            .strip()
            .lower()
        )
        if confirm != "y":
            print("Cancelled")
            sys.exit(0)
        asyncio.run(drop_all())
        sys.exit(0)

    elif command == "help" or command in ["-h", "--help"]:
        print_help()
        sys.exit(0)

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)

    sys.exit(exit_code)


def print_help() -> None:
    """Print help message."""
    print("""
Alembic Migration Helper

Usage:
  python scripts/db_migrate.py <command> [args]

Commands:
  status              Show current migration state
  upgrade [target]    Apply migrations up to target (default: head)
  downgrade [target]  Downgrade migrations to target (default: -1)
  reset               Reset database to initial state (dev only)
  sql [target]        Generate SQL for migrations without applying
  history             Show migration history
  current             Show current migration
  revision <msg>      Create new migration with auto-detect
  init-fallback       Create tables from model metadata (no tracking)
  drop-all            Drop all tables (dev only, dangerous!)
  help                Show this help message

Examples:
  python scripts/db_migrate.py status
  python scripts/db_migrate.py upgrade head
  python scripts/db_migrate.py downgrade -1
  python scripts/db_migrate.py revision "add email to users"
  python scripts/db_migrate.py reset
  python scripts/db_migrate.py sql > migration.sql

Database Configuration:
  Set DB_BACKEND environment variable to "sqlite" or "postgresql"
  Set SQLITE_PATH for SQLite (default: ./data/commclient.db)
  Set DATABASE_URL for PostgreSQL (e.g., postgresql+asyncpg://user:pass@host/db)
""")


if __name__ == "__main__":
    main()
