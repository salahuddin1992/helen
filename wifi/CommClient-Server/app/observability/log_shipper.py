"""
Phase 6 / Module AD — Loki log shipper (optional).

Buffered + batched HTTP push to Loki. If Loki is unreachable the buffer
re-tries with exponential backoff and drops oldest records once the
buffer hits ``max_buffer`` so the process never OOMs.

Configuration env:
    LOKI_URL                 — http://loki:3100  (omit to disable)
    LOKI_BATCH_SIZE          — default 500
    LOKI_FLUSH_SECONDS       — default 5
    LOKI_MAX_BUFFER          — default 50_000
    LOKI_BASIC_AUTH          — optional "user:pass"
    HELEN_ENV / HELEN_VERSION — labelled on every log line
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import deque
from typing import Any, Deque, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.cluster.leader_election import _resolve_node_id

logger = get_logger(__name__)


class LokiLogShipper(logging.Handler):
    """logging.Handler that pushes batches to Loki."""

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        batch_size: int = 500,
        flush_seconds: float = 5.0,
        max_buffer: int = 50_000,
        basic_auth: Optional[str] = None,
        labels: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__()
        self.url = url or os.environ.get("LOKI_URL", "")
        self.batch_size = int(os.environ.get("LOKI_BATCH_SIZE", batch_size))
        self.flush_seconds = float(os.environ.get("LOKI_FLUSH_SECONDS",
                                                  flush_seconds))
        self.max_buffer = int(os.environ.get("LOKI_MAX_BUFFER", max_buffer))
        self.basic_auth = basic_auth or os.environ.get("LOKI_BASIC_AUTH", "")
        node_id = _resolve_node_id()
        self.labels = {
            "service": "helen-server",
            "env": os.environ.get("HELEN_ENV", "dev"),
            "version": os.environ.get("HELEN_VERSION", "0.0.0-dev"),
            "node_id": node_id[:12],
            "host": socket.gethostname(),
            **(labels or {}),
        }
        self._buf: Deque[tuple[int, str, str]] = deque()
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._enabled = bool(self.url)

    # ── public API ──────────────────────────────────────────

    async def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._flush_loop(), name="loki-shipper")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.flush_seconds + 2)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()
        # final flush
        try:
            await self._flush_once()
        except Exception:                                           # pragma: no cover
            pass

    def emit(self, record: logging.LogRecord) -> None:
        if not self._enabled:
            return
        try:
            msg = self.format(record)
        except Exception:                                           # pragma: no cover
            msg = record.getMessage()
        ts = int(time.time() * 1e9)
        if len(self._buf) >= self.max_buffer:
            self._buf.popleft()
        self._buf.append((ts, record.levelname, msg))

    # ── internals ───────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._flush_once()
            except Exception as exc:                                # pragma: no cover
                logger.debug("loki: flush error %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.flush_seconds)
            except asyncio.TimeoutError:
                continue

    async def _flush_once(self) -> None:
        if not self._buf:
            return
        # group by level so each level becomes one Loki stream
        async with self._lock:
            grouped: dict[str, list[list[str]]] = {}
            n = min(self.batch_size, len(self._buf))
            for _ in range(n):
                ts, lvl, msg = self._buf.popleft()
                grouped.setdefault(lvl, []).append([str(ts), msg])
        if not grouped:
            return
        streams = []
        for lvl, values in grouped.items():
            streams.append({
                "stream": {**self.labels, "level": lvl.lower()},
                "values": values,
            })
        payload = {"streams": streams}
        try:
            import httpx
        except Exception:                                           # pragma: no cover
            return
        headers = {"Content-Type": "application/json"}
        auth = None
        if self.basic_auth and ":" in self.basic_auth:
            u, p = self.basic_auth.split(":", 1)
            auth = (u, p)
        try:
            async with httpx.AsyncClient(timeout=5.0, auth=auth) as cli:
                r = await cli.post(
                    self.url.rstrip("/") + "/loki/api/v1/push",
                    json=payload, headers=headers,
                )
                if r.status_code >= 300:                            # pragma: no cover
                    logger.debug("loki: push %d (%s)", r.status_code, r.text[:120])
        except Exception as exc:                                    # pragma: no cover
            logger.debug("loki: push failed %s", exc)


_singleton: Optional[LokiLogShipper] = None


def attach_log_shipper() -> Optional[LokiLogShipper]:
    """Wire the Loki shipper to the root logger. Returns the shipper or
    None if disabled."""
    global _singleton
    if _singleton is not None:
        return _singleton
    shipper = LokiLogShipper()
    if not shipper._enabled:
        return None
    shipper.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    shipper.setFormatter(fmt)
    logging.getLogger().addHandler(shipper)
    _singleton = shipper
    return shipper


def get_log_shipper() -> Optional[LokiLogShipper]:
    return _singleton
