"""
Alembic environment configuration for CommClient-Server.

Supports:
- Offline mode (SQL script generation without live DB connection)
- Online mode with async SQLAlchemy engine (both SQLite and PostgreSQL)
- Automatic model discovery from app.models
"""

from __future__ import annotations

import logging
import asyncio
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import context

# Import Base and all models so SQLAlchemy discovers table metadata
from app.db.base import Base
from app.models import (
    User,
    UserSession,
    Contact,
    Channel,
    ChannelMember,
    Message,
    Reaction,
    FileRecord,
    CallLog,
    MessageReceipt,
    Notification,
    VoiceMessage,
    IdentityKey,
    SignedPreKey,
    OneTimePreKey,
    E2EESession,
    WhiteboardSession,
    WhiteboardStroke,
    WhiteboardSnapshot,
    MediaItem,
    MediaAlbum,
    MediaAlbumItem,
    FileTransfer,
    SharedFolder,
    SharedFolderFile,
    MediaPolicy,
    UserMediaOverride,
    IngestSource,
)

from app.core.config import get_settings

# this is the Alembic Config object, which provides
# the values of the [alembic] section of the alembic.ini
# file as Python dictionary for use in process_initialization
# and other helper functions.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well. By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    settings = get_settings()
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = settings.db_url

    context.configure(
        url=configuration.get("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Execute migrations with live connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    settings = get_settings()

    # Create async engine based on backend
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = settings.db_url

    # SQLAlchemy 2.0 style async engine creation
    if settings.DB_BACKEND == "sqlite":
        connectable = create_async_engine(
            settings.db_url,
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=pool.StaticPool,
        )
    else:
        # PostgreSQL or other backends
        connectable = create_async_engine(
            settings.db_url,
            echo=False,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
        )

    async with connectable.begin() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    logger.info("Running migrations in offline mode")
    run_migrations_offline()
else:
    logger.info("Running migrations in online mode")
    asyncio.run(run_migrations_online())
