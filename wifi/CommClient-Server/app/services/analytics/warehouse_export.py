"""
Warehouse exporters.

Three optional backends, each guarded by import availability:

  * BigQuery   — ``google-cloud-bigquery``
  * Snowflake  — ``snowflake-connector-python``
  * S3 Parquet — ``pyarrow`` + ``boto3``

If a backend is missing, its exporter degrades to "noop" so calls remain
side-effect-free instead of raising.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.analytics import AnalyticsEvent

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Optional deps
# ───────────────────────────────────────────────────────────────────────


try:                                                                  # pragma: no cover
    from google.cloud import bigquery as _bq           # type: ignore[import-untyped]
    _BQ_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _bq = None                                                        # type: ignore[assignment]
    _BQ_AVAILABLE = False

try:                                                                  # pragma: no cover
    import snowflake.connector as _sf                  # type: ignore[import-untyped]
    _SF_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _sf = None                                                        # type: ignore[assignment]
    _SF_AVAILABLE = False

try:                                                                  # pragma: no cover
    import pyarrow as _pa                              # type: ignore[import-untyped]
    import pyarrow.parquet as _pq                       # type: ignore[import-untyped]
    _PA_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _pa = None                                                        # type: ignore[assignment]
    _pq = None                                                        # type: ignore[assignment]
    _PA_AVAILABLE = False

try:                                                                  # pragma: no cover
    import boto3                                       # type: ignore[import-untyped]
    _BOTO_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    boto3 = None                                                      # type: ignore[assignment]
    _BOTO_AVAILABLE = False


# ───────────────────────────────────────────────────────────────────────
# Result shape
# ───────────────────────────────────────────────────────────────────────


@dataclass
class ExportResult:
    backend: str
    ok: bool
    rows: int = 0
    bytes: int = 0
    path: Optional[str] = None
    error: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────
# Common: serialize a window of events
# ───────────────────────────────────────────────────────────────────────


async def _fetch_events_window(
    db: AsyncSession, *, workspace_id: Optional[str],
    since: datetime, until: datetime,
) -> list[dict[str, Any]]:
    q = select(AnalyticsEvent).where(and_(
        AnalyticsEvent.occurred_at >= since,
        AnalyticsEvent.occurred_at < until,
    ))
    if workspace_id:
        q = q.where(AnalyticsEvent.workspace_id == workspace_id)
    q = q.order_by(AnalyticsEvent.occurred_at)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": r.id, "workspace_id": r.workspace_id,
            "user_id": r.user_id, "session_id": r.session_id,
            "event_name": r.event_name, "properties": r.properties or {},
            "ip": r.ip, "user_agent": r.user_agent,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
        }
        for r in rows
    ]


# ───────────────────────────────────────────────────────────────────────
# Exporters
# ───────────────────────────────────────────────────────────────────────


def _bigquery_export(rows: list[dict[str, Any]], *, dataset: str, table: str,
                     project: Optional[str] = None) -> ExportResult:
    if not _BQ_AVAILABLE:
        return ExportResult("bigquery", False, error="google-cloud-bigquery missing")
    try:
        client = _bq.Client(project=project)                            # type: ignore[union-attr]
        full_id = f"{project or client.project}.{dataset}.{table}"
        errors = client.insert_rows_json(full_id, rows)
        if errors:
            return ExportResult("bigquery", False, rows=len(rows), error=str(errors))
        return ExportResult("bigquery", True, rows=len(rows), path=full_id)
    except Exception as e:                                              # noqa: BLE001
        return ExportResult("bigquery", False, error=str(e))


def _snowflake_export(rows: list[dict[str, Any]], *, account: str,
                      user: str, password: str, warehouse: str,
                      database: str, schema: str, table: str) -> ExportResult:
    if not _SF_AVAILABLE:
        return ExportResult("snowflake", False, error="snowflake-connector-python missing")
    try:
        conn = _sf.connect(                                             # type: ignore[union-attr]
            account=account, user=user, password=password,
            warehouse=warehouse, database=database, schema=schema,
        )
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id STRING, workspace_id STRING, user_id STRING, session_id STRING, "
            "event_name STRING, properties VARIANT, ip STRING, user_agent STRING, "
            "occurred_at TIMESTAMP_TZ, ingested_at TIMESTAMP_TZ)"
        )
        for r in rows:
            cur.execute(
                f"INSERT INTO {table} SELECT %s,%s,%s,%s,%s,PARSE_JSON(%s),%s,%s,%s,%s",
                (r["id"], r["workspace_id"], r["user_id"], r["session_id"],
                 r["event_name"], _json_dump(r["properties"]),
                 r["ip"], r["user_agent"], r["occurred_at"], r["ingested_at"]),
            )
        conn.commit()
        cur.close()
        conn.close()
        return ExportResult("snowflake", True, rows=len(rows), path=table)
    except Exception as e:                                              # noqa: BLE001
        return ExportResult("snowflake", False, error=str(e))


def _s3_parquet_export(rows: list[dict[str, Any]], *, bucket: str,
                       key_prefix: str = "analytics/") -> ExportResult:
    if not _PA_AVAILABLE:
        return ExportResult("s3_parquet", False, error="pyarrow missing")
    try:
        if not rows:
            return ExportResult("s3_parquet", True, rows=0)
        # Normalize properties to JSON-string for parquet schema
        for r in rows:
            r["properties"] = _json_dump(r.get("properties") or {})
        table = _pa.Table.from_pylist(rows)                              # type: ignore[union-attr]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".parquet")
        try:
            _pq.write_table(table, tmp.name, compression="snappy")        # type: ignore[union-attr]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            key = f"{key_prefix.rstrip('/')}/events-{stamp}.parquet"
            if _BOTO_AVAILABLE:
                boto3.client("s3").upload_file(tmp.name, bucket, key)    # type: ignore[union-attr]
                return ExportResult(
                    "s3_parquet", True, rows=len(rows),
                    bytes=Path(tmp.name).stat().st_size,
                    path=f"s3://{bucket}/{key}",
                )
            return ExportResult(
                "s3_parquet", True, rows=len(rows),
                bytes=Path(tmp.name).stat().st_size,
                path=tmp.name, error="boto3-missing-local-only",
            )
        finally:
            try:
                Path(tmp.name).unlink()
            except Exception:                                           # noqa: BLE001
                pass
    except Exception as e:                                              # noqa: BLE001
        return ExportResult("s3_parquet", False, error=str(e))


def _json_dump(v: Any) -> str:
    import json
    try:
        return json.dumps(v, default=str)
    except Exception:                                                   # noqa: BLE001
        return "{}"


# ───────────────────────────────────────────────────────────────────────
# Public scheduled-export API
# ───────────────────────────────────────────────────────────────────────


async def export_window(
    *, backend: str, config: dict[str, Any],
    workspace_id: Optional[str] = None,
    since: datetime, until: datetime,
) -> ExportResult:
    async with async_session_factory() as db:
        rows = await _fetch_events_window(
            db, workspace_id=workspace_id, since=since, until=until,
        )
    if backend == "bigquery":
        return _bigquery_export(
            rows, dataset=config["dataset"], table=config["table"],
            project=config.get("project"),
        )
    if backend == "snowflake":
        return _snowflake_export(rows, **{
            k: config[k] for k in ("account", "user", "password",
                                   "warehouse", "database", "schema", "table")
        })
    if backend == "s3_parquet":
        return _s3_parquet_export(
            rows, bucket=config["bucket"],
            key_prefix=config.get("key_prefix", "analytics/"),
        )
    return ExportResult(backend, False, error=f"unknown backend: {backend}")


async def schedule_daily_export(
    *, backend: str, config: dict[str, Any],
    workspace_id: Optional[str] = None,
    interval_seconds: int = 86_400,
) -> None:
    """Background loop that exports the previous day every interval."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            until = now.replace(hour=0, minute=0, second=0, microsecond=0)
            since = until - timedelta(days=1)
            result = await export_window(
                backend=backend, config=config,
                workspace_id=workspace_id, since=since, until=until,
            )
            logger.info("warehouse.export backend=%s ok=%s rows=%s err=%s",
                        backend, result.ok, result.rows, result.error)
        except Exception as e:                                          # noqa: BLE001
            logger.error("warehouse.export crashed: %s", e)
        await asyncio.sleep(interval_seconds)
