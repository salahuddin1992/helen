"""
Structured logging configuration using structlog.
All log output is JSON in production, pretty-printed in debug mode.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

import structlog

from app.core.config import get_settings

# Rotation: 1 GB per file, keep 5 rolls (≈5 GB worst case on disk).
_LOG_FILE_NAME = "helen-server.log"
_LOG_FILE_MAX_BYTES = 1024 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 5


def setup_logging() -> None:
    """Configure structured logging for the application."""
    settings = get_settings()
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Shared processors for both structlog and stdlib
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.DEBUG:
        # Pretty console output for development
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # JSON output for production
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging to use structlog formatting
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)
    root_logger.setLevel(log_level)

    # File handler — always JSON, regardless of DEBUG. An operator staring at
    # a crash file at 2 AM wants grep-able structured fields, not ANSI colors.
    try:
        log_dir = settings.log_path
        file_handler = RotatingFileHandler(
            log_dir / _LOG_FILE_NAME,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
    except OSError:
        # Read-only install, denied permissions, or path unresolvable —
        # logging to stdout still works, so don't block startup.
        pass

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named structured logger."""
    return structlog.get_logger(name)
