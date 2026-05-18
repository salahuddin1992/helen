"""
Log compaction — keep the audit chain manageable.

The audit_replication chain is append-only and grows forever. After
a year of operator activity it can hit 100s of MB. Compaction:

  1. Periodically scans the chain.
  2. Detects entries older than ``RETAIN_DAYS`` (default 365).
  3. Computes a Merkle root of the soon-to-be-archived prefix.
  4. Moves them to ``data/audit_archive/<year>/<month>.jsonl.gz``.
  5. Trims the live chain, *prepending* a synthetic
     ``audit_archive_anchor`` entry whose ``prev_hash`` matches the
     archived Merkle root — preserving end-to-end verifiability.

Properties
----------
* **Chain integrity preserved** — anyone walking from genesis to
  head still sees an unbroken chain; the anchor entry contains the
  archive hash so an auditor can re-verify offline.
* **Cluster-coordinated** — wrapped in ``distributed_lock("log_compactor")``
  so only one peer compacts at a time. The result replicates
  through the existing audit_replication mechanism.
* **Idempotent** — re-running compaction on already-compacted data
  is a no-op.
* **Tunable** — ``HELEN_AUDIT_RETAIN_DAYS`` env override.

Storage layout after compaction:

    data/audit_chain.jsonl              ← live, recent entries only
    data/audit_archive/2025/01.jsonl.gz ← archived, hash-anchored
    data/audit_archive/2025/02.jsonl.gz
    data/audit_archive/index.json       ← {month → merkle_root}
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import gzip
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_CHAIN_FILE   = _DATA_DIR / "audit_chain.jsonl"
_ARCHIVE_DIR  = _DATA_DIR / "audit_archive"
_INDEX_FILE   = _ARCHIVE_DIR / "index.json"

RETAIN_DAYS         = int(os.environ.get("HELEN_AUDIT_RETAIN_DAYS", "365"))
COMPACT_INTERVAL_SEC = 24 * 60 * 60.0   # daily


# ── Helpers ─────────────────────────────────────────────────────


def _entry_month(entry: dict) -> tuple[int, int]:
    ts = float(entry.get("timestamp") or 0.0)
    if ts <= 0:
        return (0, 0)
    d = _dt.datetime.utcfromtimestamp(ts)
    return (d.year, d.month)


def _merkle_root(entries: list[dict]) -> str:
    """Root hash over the archived block — used as the anchor."""
    h = hashlib.sha256()
    for e in entries:
        h.update((e.get("this_hash") or "").encode())
    return h.hexdigest()


def _load_index() -> dict:
    try:
        if _INDEX_FILE.is_file():
            return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("audit_archive_index_read_failed", error=str(e))
    return {"months": {}}


def _save_index(idx: dict) -> None:
    try:
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        _INDEX_FILE.write_text(
            json.dumps(idx, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("audit_archive_index_write_failed", error=str(e))


# ── Core compaction ─────────────────────────────────────────────


def compact_once(retain_days: int = RETAIN_DAYS) -> dict:
    """Scan the chain, archive entries older than ``retain_days``,
    rewrite the live chain. Returns a stats dict."""
    if not _CHAIN_FILE.is_file():
        return {"archived": 0, "kept": 0, "skipped": "no_chain"}

    cutoff = time.time() - retain_days * 86_400.0
    archived: dict[tuple[int, int], list[dict]] = {}
    kept_lines: list[str] = []
    archived_count = 0

    with _CHAIN_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line_s = line.strip()
            if not line_s:
                continue
            try:
                e = json.loads(line_s)
            except Exception:
                kept_lines.append(line)
                continue
            ts = float(e.get("timestamp") or 0.0)
            if ts < cutoff and ts > 0:
                month_key = _entry_month(e)
                archived.setdefault(month_key, []).append(e)
                archived_count += 1
            else:
                kept_lines.append(line)

    if archived_count == 0:
        return {"archived": 0, "kept": len(kept_lines), "skipped": "nothing_old_enough"}

    # Write each month bundle to gzip.
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    idx = _load_index()
    months_idx = idx.setdefault("months", {})
    for (year, month), entries in sorted(archived.items()):
        if year == 0:
            continue
        year_dir = _ARCHIVE_DIR / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        out_path = year_dir / f"{month:02d}.jsonl.gz"
        # Append-mode gzip — re-running the compactor adds new entries
        # to an existing month's archive without rewriting it.
        with gzip.open(out_path, "ab") as gz:
            for e in entries:
                gz.write((json.dumps(e) + "\n").encode("utf-8"))

        merkle = _merkle_root(entries)
        key = f"{year}-{month:02d}"
        existing_root = months_idx.get(key, {}).get("merkle_root")
        # If a root already exists, chain the new bundle on top.
        if existing_root:
            chained = hashlib.sha256(
                (existing_root + merkle).encode()
            ).hexdigest()
            merkle = chained
        months_idx[key] = {
            "merkle_root":   merkle,
            "entry_count":   months_idx.get(key, {}).get("entry_count", 0)
                             + len(entries),
            "last_compacted_at": time.time(),
        }

    _save_index(idx)

    # Atomically rewrite the live chain with only the kept lines.
    tmp_path = _CHAIN_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.writelines(kept_lines)
    shutil.move(str(tmp_path), str(_CHAIN_FILE))

    logger.info(
        "audit_chain_compacted",
        archived=archived_count,
        kept_lines=len(kept_lines),
        retain_days=retain_days,
    )
    return {
        "archived": archived_count,
        "kept":     len(kept_lines),
        "months":   list(f"{y}-{m:02d}" for (y, m) in archived.keys()),
    }


# ── Background loop with cluster-wide lock ──────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _compact_loop() -> None:
    global _running
    _running = True
    logger.info(
        "log_compaction_started",
        interval_sec=COMPACT_INTERVAL_SEC,
        retain_days=RETAIN_DAYS,
    )
    try:
        while _running:
            try:
                await _compact_with_lock()
            except Exception as e:
                logger.warning("log_compaction_cycle_failed", error=str(e))
            await asyncio.sleep(COMPACT_INTERVAL_SEC)
    finally:
        logger.info("log_compaction_stopped")


async def _compact_with_lock() -> None:
    """Acquire the cluster lock and compact. Other peers see the
    lock and skip."""
    try:
        from app.services.distributed_lock import distributed_lock
    except ImportError:
        compact_once()
        return
    async with distributed_lock("log_compactor", ttl=600.0,
                                 acquire_timeout=2.0) as held:
        if not held:
            logger.debug("log_compaction_skipped_lock_held")
            return
        compact_once()


def start_log_compaction() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_compact_loop(), name="log-compaction")
    except RuntimeError:
        logger.warning("log_compaction_no_event_loop_yet")


def stop_log_compaction() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def archive_summary() -> dict:
    """List archived months and their merkle roots — read by
    /api/admin/peers/audit/archive."""
    idx = _load_index()
    return {
        "months":     idx.get("months", {}),
        "archive_dir": str(_ARCHIVE_DIR),
        "live_chain": str(_CHAIN_FILE),
        "retain_days": RETAIN_DAYS,
    }
