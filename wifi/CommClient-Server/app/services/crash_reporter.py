"""
Local crash reporter — Sentry-style without the cloud.

Captures unhandled exceptions, environment context, and recent log
breadcrumbs into a SQLite store the admin can browse via the
`/api/admin/crashes` endpoint. No telemetry leaves the LAN.

Wire-up
-------
Install once during app boot::

    from app.services.crash_reporter import install_crash_reporter
    install_crash_reporter(data_dir="/opt/helen-server/_internal/data")

After that:

* Every uncaught exception in any thread / task is recorded.
* FastAPI's exception middleware can call ``capture(exc, context)``
  to log handled-but-suspicious errors with request context.
* Background tasks can call ``capture_event(level, message, **ctx)``
  for structured warnings that aren't exceptions.

Privacy
-------
Stack frames are stored as raw text. Local-vars are NOT recorded
(prevents leaking JWT tokens, message contents, etc.). The hostname,
process pid, and OS version are stored — useful for "is this a
multi-host bug or a single-machine fluke?".
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Storage ────────────────────────────────────────────────────────


@dataclass
class CrashEvent:
    event_id: str
    timestamp: float
    level: str                      # crash | error | warning | info
    type: str                       # ExceptionType or "event"
    message: str
    stack_trace: str = ""
    hostname: str = ""
    pid: int = 0
    os: str = ""
    helen_version: str = ""
    breadcrumbs: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=dict)


class CrashStore:

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS crash_events (
                    event_id      TEXT PRIMARY KEY,
                    timestamp     REAL NOT NULL,
                    level         TEXT NOT NULL,
                    type          TEXT NOT NULL,
                    message       TEXT NOT NULL,
                    stack_trace   TEXT,
                    hostname      TEXT,
                    pid           INTEGER,
                    os            TEXT,
                    helen_version TEXT,
                    breadcrumbs_json TEXT,
                    context_json  TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_crash_ts "
                       "ON crash_events(timestamp)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_crash_level "
                       "ON crash_events(level)")

    def save(self, e: CrashEvent) -> None:
        with self._lock, sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO crash_events VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e.event_id, e.timestamp, e.level, e.type, e.message,
                    e.stack_trace, e.hostname, e.pid, e.os,
                    e.helen_version,
                    json.dumps(e.breadcrumbs, ensure_ascii=False),
                    json.dumps(e.context, ensure_ascii=False),
                ),
            )

    def list_recent(self, limit: int = 100,
                     level: Optional[str] = None) -> list[dict]:
        with sqlite3.connect(self.db_path) as c:
            if level:
                rows = c.execute(
                    "SELECT * FROM crash_events WHERE level=? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (level, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM crash_events "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, event_id: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT * FROM crash_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def purge_older_than(self, days: int = 30) -> int:
        cutoff = time.time() - (days * 86400)
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute(
                "DELETE FROM crash_events WHERE timestamp < ?", (cutoff,),
            )
            return cur.rowcount

    def _row_to_dict(self, row: tuple) -> dict:
        return {
            "event_id": row[0], "timestamp": row[1], "level": row[2],
            "type": row[3], "message": row[4], "stack_trace": row[5],
            "hostname": row[6], "pid": row[7], "os": row[8],
            "helen_version": row[9],
            "breadcrumbs": json.loads(row[10]) if row[10] else [],
            "context": json.loads(row[11]) if row[11] else {},
        }


# ── Capturing ──────────────────────────────────────────────────────


class CrashReporter:

    BREADCRUMB_LIMIT = 50           # most recent log lines kept

    def __init__(self, store: CrashStore,
                 helen_version: str = "1.0.0") -> None:
        self.store = store
        self.helen_version = helen_version
        self._breadcrumbs: deque = deque(maxlen=self.BREADCRUMB_LIMIT)
        self._lock = threading.Lock()
        self._hostname = socket.gethostname()
        self._os = f"{platform.system()} {platform.release()}"
        self._installed = False

    def add_breadcrumb(self, category: str, message: str,
                       data: Optional[dict] = None) -> None:
        with self._lock:
            self._breadcrumbs.append({
                "ts": time.time(),
                "category": category,
                "message": message,
                "data": data or {},
            })

    def capture_exception(
        self, exc: BaseException,
        *,
        context: Optional[dict] = None,
        level: str = "error",
    ) -> str:
        evt = CrashEvent(
            event_id=_short_id(),
            timestamp=time.time(),
            level=level,
            type=type(exc).__name__,
            message=str(exc) or repr(exc),
            stack_trace="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__),
            ),
            hostname=self._hostname,
            pid=os.getpid(),
            os=self._os,
            helen_version=self.helen_version,
            breadcrumbs=list(self._breadcrumbs),
            context=_sanitize_context(context or {}),
        )
        self.store.save(evt)
        return evt.event_id

    def capture_event(
        self, level: str, message: str, **context: Any,
    ) -> str:
        evt = CrashEvent(
            event_id=_short_id(),
            timestamp=time.time(),
            level=level,
            type="event",
            message=message,
            hostname=self._hostname,
            pid=os.getpid(),
            os=self._os,
            helen_version=self.helen_version,
            breadcrumbs=list(self._breadcrumbs),
            context=_sanitize_context(context),
        )
        self.store.save(evt)
        return evt.event_id

    def install(self) -> None:
        """Wire ourselves into sys.excepthook and threading.excepthook
        so any unhandled exception becomes a saved event."""
        if self._installed:
            return
        self._installed = True

        prev_excepthook = sys.excepthook

        def _hook(exc_type, exc, tb):
            try:
                self.capture_exception(exc, level="crash")
            except Exception:
                pass
            prev_excepthook(exc_type, exc, tb)

        sys.excepthook = _hook

        # Thread-local exceptions
        if hasattr(threading, "excepthook"):
            prev_thread_hook = threading.excepthook

            def _thread_hook(args):
                try:
                    self.capture_exception(
                        args.exc_value, level="crash",
                        context={"thread": args.thread.name},
                    )
                except Exception:
                    pass
                prev_thread_hook(args)

            threading.excepthook = _thread_hook


def _sanitize_context(ctx: dict) -> dict:
    """Strip tokens and secrets that callers might naively include
    in context dicts."""
    SENSITIVE = ("password", "token", "secret", "authorization",
                  "cookie", "session", "jwt")
    out = {}
    for k, v in ctx.items():
        kl = str(k).lower()
        if any(s in kl for s in SENSITIVE):
            out[k] = "<redacted>"
        elif isinstance(v, dict):
            out[k] = _sanitize_context(v)
        else:
            out[k] = v
    return out


def _short_id() -> str:
    import secrets
    return secrets.token_urlsafe(8)


# ── Module-level singleton + installer ─────────────────────────────


_REPORTER: Optional[CrashReporter] = None


def install_crash_reporter(
    data_dir: str,
    helen_version: str = "1.0.0",
) -> CrashReporter:
    global _REPORTER
    if _REPORTER is not None:
        return _REPORTER
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "crashes.db")
    store = CrashStore(db_path)
    _REPORTER = CrashReporter(store, helen_version=helen_version)
    _REPORTER.install()
    return _REPORTER


def get_reporter() -> Optional[CrashReporter]:
    return _REPORTER
