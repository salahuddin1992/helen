"""Metrics history — SQLite-backed time-series persistence.

The rolling window in monitoring.metrics_collector keeps recent
state for live dashboards, but capacity planning and forecasting
need historical data — "what was CPU yesterday at 14:00?".

This module writes a downsampled sample of the live metrics every
``SAMPLE_INTERVAL_SEC`` (default 60s) into ``data/metrics_history.sqlite``
with the schema:

    CREATE TABLE samples (
        ts        REAL    PRIMARY KEY,  -- unix seconds
        metric    TEXT    NOT NULL,
        value     REAL    NOT NULL,
        node_id   TEXT
    );
    CREATE INDEX idx_samples_metric_ts ON samples(metric, ts);

Retention: rows older than ``RETENTION_DAYS`` (default 30) are
pruned at the end of each sample cycle.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


SAMPLE_INTERVAL_SEC = _f("HELEN_METRICS_HISTORY_SEC", 60.0)
RETENTION_DAYS      = _i("HELEN_METRICS_RETENTION_DAYS", 30)

_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_DB_PATH  = _DATA_DIR / "metrics_history.sqlite"


class MetricsHistory:
    _singleton: "MetricsHistory | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "MetricsHistory":
        if cls._singleton is None:
            cls._singleton = MetricsHistory()
        return cls._singleton

    # ── Connection ────────────────────────────────────────

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(
            str(_DB_PATH), check_same_thread=False, isolation_level=None,
        )
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                ts      REAL    NOT NULL,
                metric  TEXT    NOT NULL,
                value   REAL    NOT NULL,
                node_id TEXT,
                PRIMARY KEY (ts, metric)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_metric_ts "
            "ON samples(metric, ts)"
        )
        self._conn = c
        return c

    # ── Write ─────────────────────────────────────────────

    def record(self, metric: str, value: float,
               *, ts: float | None = None) -> None:
        try:
            from app.services.discovery_service import get_server_id
            node_id = get_server_id() or "anon"
        except Exception:
            node_id = "anon"
        ts = ts if ts is not None else time.time()
        try:
            with self._lock:
                self._ensure_conn().execute(
                    "INSERT OR REPLACE INTO samples(ts, metric, value, node_id) "
                    "VALUES (?, ?, ?, ?)",
                    (float(ts), str(metric), float(value), node_id),
                )
        except Exception as e:
            logger.debug("metrics_history_write_failed", error=str(e)[:80])

    def record_many(self, samples: list[tuple[str, float]]) -> int:
        ts = time.time()
        try:
            from app.services.discovery_service import get_server_id
            node_id = get_server_id() or "anon"
        except Exception:
            node_id = "anon"
        rows = [(ts, m, float(v), node_id) for m, v in samples]
        if not rows:
            return 0
        try:
            with self._lock:
                self._ensure_conn().executemany(
                    "INSERT OR REPLACE INTO samples(ts, metric, value, node_id) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
        except Exception as e:
            logger.debug("metrics_history_bulk_failed", error=str(e)[:80])
            return 0
        return len(rows)

    # ── Read ──────────────────────────────────────────────

    def query(self, metric: str,
              *, since_ts: float = 0,
              until_ts: float | None = None,
              limit: int = 5000) -> list[dict]:
        if until_ts is None:
            until_ts = time.time() + 1
        try:
            with self._lock:
                rows = self._ensure_conn().execute(
                    "SELECT ts, value, node_id FROM samples "
                    "WHERE metric = ? AND ts >= ? AND ts <= ? "
                    "ORDER BY ts LIMIT ?",
                    (metric, float(since_ts), float(until_ts), int(limit)),
                ).fetchall()
        except Exception:
            return []
        return [
            {"ts": r[0], "value": r[1], "node_id": r[2]}
            for r in rows
        ]

    def metrics(self) -> list[str]:
        try:
            with self._lock:
                rows = self._ensure_conn().execute(
                    "SELECT DISTINCT metric FROM samples ORDER BY metric"
                ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def prune(self) -> int:
        cutoff = time.time() - RETENTION_DAYS * 86400.0
        try:
            with self._lock:
                cur = self._ensure_conn().execute(
                    "DELETE FROM samples WHERE ts < ?", (cutoff,),
                )
                return getattr(cur, "rowcount", 0) or 0
        except Exception:
            return 0

    # ── Sampling loop ────────────────────────────────────

    def _sample_now(self) -> int:
        """Pull from monitoring.metrics_collector + record the
        scalar leaves we want to retain."""
        samples: list[tuple[str, float]] = []
        try:
            from app.monitoring.metrics_collector import get_metrics_collector
            blob = get_metrics_collector().latest()
        except Exception:
            blob = {}

        bp = (blob.get("backpressure") or {})
        if "saturation" in bp:
            samples.append(("backpressure_saturation", float(bp["saturation"])))
        pt = blob.get("partition") or {}
        if "fresh_count" in pt:
            samples.append(("partition_fresh_count", float(pt["fresh_count"])))

        # Distributed snapshot has node load + capacity.
        ds = blob.get("distributed") or {}
        if isinstance(ds, dict):
            replication = ds.get("replication") or {}
            if "local_records" in replication:
                samples.append(("local_records",
                                float(replication["local_records"])))

        # path_health tracked count.
        ph = blob.get("path_health") or {}
        if "tracked_count" in ph:
            samples.append(("path_health_tracked", float(ph["tracked_count"])))

        # multipath route count.
        mp = blob.get("multipath") or {}
        if "routes" in mp:
            samples.append(("multipath_route_count", float(len(mp["routes"]))))

        # Self node load.
        try:
            from app.services.node_registry import get_registry
            self_node = next(
                (n for n in get_registry().nodes(include_dead=True)
                 if n.self_node),
                None,
            )
            if self_node is not None:
                samples.append(("active_sockets", float(self_node.load.active_sockets)))
                samples.append(("active_rooms", float(self_node.load.active_rooms)))
                samples.append(("cpu_pct", float(self_node.load.cpu_pct)))
                samples.append(("rss_pct", float(self_node.load.rss_pct)))
        except Exception:
            pass

        return self.record_many(samples)

    async def _run_loop(self) -> None:
        self._running = True
        logger.info("metrics_history_started",
                    interval_sec=SAMPLE_INTERVAL_SEC)
        try:
            while self._running:
                try:
                    self._sample_now()
                    self.prune()
                except Exception as e:
                    logger.warning("metrics_history_cycle_failed",
                                   error=str(e))
                await asyncio.sleep(SAMPLE_INTERVAL_SEC)
        finally:
            logger.info("metrics_history_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="metrics-history",
            )
        except RuntimeError:
            logger.warning("metrics_history_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ──────────────────────────────────────

    def stats(self) -> dict:
        try:
            with self._lock:
                row = self._ensure_conn().execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples"
                ).fetchone()
            count, min_ts, max_ts = row
            return {
                "running":          self._running,
                "interval_sec":     SAMPLE_INTERVAL_SEC,
                "retention_days":   RETENTION_DAYS,
                "row_count":        int(count or 0),
                "earliest_ts":      min_ts,
                "latest_ts":        max_ts,
                "metrics_distinct": len(self.metrics()),
                "db_path":          str(_DB_PATH),
            }
        except Exception as e:
            return {"error": str(e)[:80]}


def get_metrics_history() -> MetricsHistory:
    return MetricsHistory.instance()
