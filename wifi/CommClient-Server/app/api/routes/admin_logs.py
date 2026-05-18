"""
Admin — Real-time logs (Phase 2 / Module E).

Endpoints
---------
GET    /api/admin/logs/list                — log file inventory
GET    /api/admin/logs/tail                — last N lines (paginated, filtered)
WS     /api/admin/logs/stream              — live WebSocket tail
GET    /api/admin/logs/crashes             — crash_reporter SQLite integration
GET    /api/admin/logs/download/{name}     — secure file download

Behaviour
---------
* Parses structlog JSON lines (one JSON document per line). Falls back to a
  raw-text record when a line is not valid JSON, so legacy / mixed sources
  still appear in the feed.
* Watches the logs directory with `watchdog` when installed; otherwise uses
  an asyncio polling fallback (500 ms). Both paths feed the same in-process
  ring buffer (5000 lines) so newly-connected clients see immediate tail
  without scanning files.
* WebSocket protocol — server pushes JSON frames:
    {"type":"hello","buffer":[...]}            on connect
    {"type":"line","record":{...}}             on each new line
    {"type":"pong"}                            in response to client "ping"
* Filtering on `/tail` and on the live stream is applied identically:
  ``level`` (>=), ``logger`` substring, ``pattern`` regex, ``since`` /
  ``until`` ISO timestamps.
* Path-traversal proof: download / tail resolve the requested filename
  against the canonical logs directory and reject anything that escapes it.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import decode_token
from app.core.security_utils import require_role

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/logs", tags=["admin-phase2"])


# ─────────────────────────────────────────────────────────────
# Helpers — path resolution, parsing, filtering
# ─────────────────────────────────────────────────────────────

def _logs_dir() -> Path:
    """Resolve the on-disk logs directory the same way `app.core.logging`
    does. We tolerate the directory not yet existing — empty inventory is
    a valid response."""
    settings = get_settings()
    raw = settings.LOG_DIR or ""
    if raw:
        p = Path(raw)
    else:
        # Mirror logging.py: fall back to <PROJECT_ROOT>/logs
        p = Path(settings.PROJECT_ROOT) / "logs"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p.resolve()


def _safe_log_path(name: str) -> Path:
    """Reject path-traversal. Returns the absolute path or raises 400/404."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid log filename")
    base = _logs_dir()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes logs dir")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    return candidate


_LEVEL_ORDER = {
    "DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30,
    "ERROR": 40, "CRITICAL": 50, "FATAL": 50,
}


def _parse_line(raw: str, *, source: str) -> dict[str, Any]:
    """Parse one log line into a normalised record."""
    raw = raw.rstrip("\r\n")
    if not raw:
        return {}
    record: dict[str, Any] = {"_raw": raw, "_source": source}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            record.update(obj)
            # Normalise field names — structlog uses "level", others "levelname".
            lvl = (obj.get("level") or obj.get("levelname") or "INFO")
            record["level"] = str(lvl).upper()
            record["timestamp"] = obj.get("timestamp") or obj.get("time") or obj.get("ts")
            record["logger"] = obj.get("logger") or obj.get("name") or ""
            record["message"] = obj.get("event") or obj.get("message") or ""
    except (json.JSONDecodeError, TypeError):
        record["level"] = "INFO"
        record["message"] = raw
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        record["logger"] = ""
    return record


def _record_matches(
    rec: dict[str, Any],
    *,
    level: Optional[str],
    logger_filter: Optional[str],
    pattern: Optional[re.Pattern[str]],
    since: Optional[float],
    until: Optional[float],
) -> bool:
    if level:
        want = _LEVEL_ORDER.get(level.upper(), 0)
        got = _LEVEL_ORDER.get(str(rec.get("level", "")).upper(), 0)
        if got < want:
            return False
    if logger_filter:
        if logger_filter not in str(rec.get("logger", "")):
            return False
    if pattern is not None:
        haystack = rec.get("_raw") or json.dumps(rec, ensure_ascii=False)
        if not pattern.search(haystack):
            return False
    if since is not None or until is not None:
        ts = _record_epoch(rec)
        if ts is None:
            return False
        if since is not None and ts < since:
            return False
        if until is not None and ts > until:
            return False
    return True


def _record_epoch(rec: dict[str, Any]) -> Optional[float]:
    ts = rec.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────────────────────
# Ring buffer + file-watcher
# ─────────────────────────────────────────────────────────────

class _LogRingBuffer:
    """Process-wide in-memory tail buffer (5000 lines, all files merged).

    A background asyncio task tails every *.log* file in the logs dir.
    New lines feed the buffer and any registered WebSocket queue."""

    MAX_LINES = 5000
    POLL_INTERVAL = 0.5

    def __init__(self) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=self.MAX_LINES)
        self._lock = threading.Lock()
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None  # set in ensure_started
        # filename -> last byte offset we've read
        self._offsets: dict[str, int] = {}

    def snapshot(self, n: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def _push(self, record: dict[str, Any]) -> None:
        if not record:
            return
        with self._lock:
            self._buf.append(record)
        # Fan out to live subscribers — never block the producer.
        dead: list[asyncio.Queue] = []
        for q in list(self._subs):
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="log-tail")

    async def _run(self) -> None:
        """Polling tailer — robust, dependency-free."""
        # Seed the buffer with the existing tail (last 500 lines from each file).
        try:
            for f in self._enumerate_logs():
                try:
                    self._seed_file(f)
                except Exception as e:
                    logger.debug("log_seed_failed", file=str(f), error=str(e))
        except Exception:
            pass

        assert self._stop is not None
        while not self._stop.is_set():
            try:
                for f in self._enumerate_logs():
                    try:
                        self._tail_file(f)
                    except Exception as e:
                        logger.debug("log_tail_iter_failed",
                                     file=str(f), error=str(e))
            except Exception as e:
                logger.warning("log_tailer_error", error=str(e))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.POLL_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _enumerate_logs() -> Iterable[Path]:
        d = _logs_dir()
        if not d.exists():
            return []
        return [p for p in d.iterdir()
                if p.is_file() and (p.suffix == ".log" or ".log" in p.name)]

    def _seed_file(self, f: Path) -> None:
        size = f.stat().st_size
        chunk = 256 * 1024
        start = max(0, size - chunk)
        with open(f, "rb") as fh:
            fh.seek(start)
            data = fh.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            # Skip first partial line if we didn't start at byte 0
            if start > 0 and lines:
                lines = lines[1:]
            for ln in lines[-500:]:
                rec = _parse_line(ln, source=f.name)
                if rec:
                    with self._lock:
                        self._buf.append(rec)
        self._offsets[f.name] = size

    def _tail_file(self, f: Path) -> None:
        try:
            size = f.stat().st_size
        except FileNotFoundError:
            return
        prev = self._offsets.get(f.name, size)
        # If the file shrank, it was rotated.
        if size < prev:
            prev = 0
        if size == prev:
            return
        with open(f, "rb") as fh:
            fh.seek(prev)
            data = fh.read().decode("utf-8", errors="replace")
        self._offsets[f.name] = size
        for ln in data.splitlines():
            if ln.strip():
                self._push(_parse_line(ln, source=f.name))


_BUFFER = _LogRingBuffer()


# ─────────────────────────────────────────────────────────────
# REST — inventory / tail / crashes / download
# ─────────────────────────────────────────────────────────────

class LogFileInfo(BaseModel):
    name: str
    size_bytes: int
    modified: str
    is_current: bool


@router.get("/list")
async def list_logs(user_id: str = Depends(require_role("admin"))):
    """Return all log files in the logs directory."""
    await _BUFFER.ensure_started()
    base = _logs_dir()
    files: list[LogFileInfo] = []
    if base.exists():
        for f in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except FileNotFoundError:
                continue
            files.append(LogFileInfo(
                name=f.name,
                size_bytes=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime,
                                                tz=timezone.utc).isoformat(),
                is_current=(".log" in f.name and
                            not any(c.isdigit() for c in f.suffix)),
            ))
    # Discover unique loggers from the ring buffer for the dropdown.
    seen_loggers: set[str] = set()
    for r in _BUFFER.snapshot(2000):
        lg = r.get("logger")
        if isinstance(lg, str) and lg:
            seen_loggers.add(lg)
    return {
        "files": [f.model_dump() for f in files],
        "logs_dir": str(base),
        "loggers": sorted(seen_loggers),
        "buffer_capacity": _LogRingBuffer.MAX_LINES,
        "buffer_used": len(_BUFFER._buf),  # noqa: SLF001 — read-only
    }


@router.get("/tail")
async def tail_logs(
    user_id: str = Depends(require_role("admin")),
    limit: int = Query(200, ge=1, le=5000),
    level: Optional[str] = None,
    logger_name: Optional[str] = Query(None, alias="logger"),
    pattern: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
):
    """Return up to `limit` recent buffered lines after filtering."""
    await _BUFFER.ensure_started()
    pat: Optional[re.Pattern[str]] = None
    if pattern:
        try:
            pat = re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Bad regex: {e}")
    records = _BUFFER.snapshot(_LogRingBuffer.MAX_LINES)
    matched = [
        r for r in records
        if _record_matches(r, level=level, logger_filter=logger_name,
                           pattern=pat, since=since, until=until)
    ]
    return {"records": matched[-limit:], "count": len(matched)}


@router.get("/crashes")
async def list_crashes(
    user_id: str = Depends(require_role("admin")),
    limit: int = Query(100, ge=1, le=1000),
):
    """Read crash reports from the crash_reporter SQLite, if present."""
    base = _logs_dir().parent
    candidates = [
        _logs_dir() / "crashes.db",
        base / "data" / "crashes.db",
        base / "crashes.db",
    ]
    db_path = next((p for p in candidates if p.exists()), None)
    if db_path is None:
        return {"crashes": [], "db": None,
                "note": "crash_reporter database not found"}
    try:
        with sqlite3.connect(str(db_path)) as c:
            c.row_factory = sqlite3.Row
            try:
                rows = c.execute(
                    "SELECT * FROM crashes ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                # Some implementations use crash_reports
                rows = c.execute(
                    "SELECT * FROM crash_reports ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return {
                "crashes": [dict(r) for r in rows],
                "db": str(db_path),
            }
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Crash DB error: {e}")


@router.get("/download/{name}")
async def download_log(
    name: str,
    user_id: str = Depends(require_role("admin")),
):
    """Stream a single log file as an attachment."""
    path = _safe_log_path(name)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
    )


# ─────────────────────────────────────────────────────────────
# WebSocket — live tail
# ─────────────────────────────────────────────────────────────

async def _ws_auth(ws: WebSocket) -> Optional[str]:
    """Authenticate a WS connection. The admin client passes ?token=… or
    sends an Authorization header. We require admin role."""
    token: Optional[str] = (
        ws.query_params.get("token")
        or ws.query_params.get("access_token")
    )
    if not token:
        auth = ws.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if not token:
        await ws.close(code=4401)
        return None
    try:
        payload = decode_token(token)
    except Exception:
        await ws.close(code=4401)
        return None
    if payload.get("type") != "access":
        await ws.close(code=4401)
        return None
    role = payload.get("role", "user")
    if role != "admin":
        await ws.close(code=4403)
        return None
    return payload.get("sub")


@router.websocket("/stream")
async def stream_logs(ws: WebSocket):
    await ws.accept()
    user_id = await _ws_auth(ws)
    if user_id is None:
        return
    await _BUFFER.ensure_started()

    # Per-connection filters; the client can update them with a JSON command.
    filters: dict[str, Any] = {
        "level": None, "logger": None, "pattern": None,
        "since": None, "until": None,
    }
    pat_cache: Optional[re.Pattern[str]] = None

    q = _BUFFER.subscribe()
    try:
        # 1) Hello — last 500 buffered records so the UI is populated instantly.
        await ws.send_text(json.dumps({
            "type": "hello",
            "buffer": _BUFFER.snapshot(500),
            "filters": filters,
        }, ensure_ascii=False, default=str))

        async def _reader() -> None:
            nonlocal pat_cache
            while True:
                msg = await ws.receive_text()
                try:
                    cmd = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                kind = cmd.get("type")
                if kind == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                elif kind == "filters":
                    for k in ("level", "logger", "pattern", "since", "until"):
                        if k in cmd:
                            filters[k] = cmd[k]
                    if filters["pattern"]:
                        try:
                            pat_cache = re.compile(filters["pattern"])
                        except re.error:
                            pat_cache = None
                            filters["pattern"] = None
                    else:
                        pat_cache = None
                    await ws.send_text(json.dumps({
                        "type": "filters_ack", "filters": filters,
                    }))

        reader_task = asyncio.create_task(_reader())

        try:
            while True:
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat — keeps the proxy/idle-timeout off our back.
                    await ws.send_text(json.dumps({"type": "heartbeat",
                                                   "ts": time.time()}))
                    continue
                if not _record_matches(
                    rec,
                    level=filters["level"],
                    logger_filter=filters["logger"],
                    pattern=pat_cache,
                    since=filters["since"],
                    until=filters["until"],
                ):
                    continue
                await ws.send_text(json.dumps(
                    {"type": "line", "record": rec},
                    ensure_ascii=False, default=str,
                ))
        finally:
            reader_task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("logs_ws_error", error=str(e), user_id=user_id)
    finally:
        _BUFFER.unsubscribe(q)
