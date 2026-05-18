"""Cluster snapshot — point-in-time state checkpoint.

Captures a coherent view of the cluster's coordination state:

  * Service registry (registered services + their health/capacity)
  * Topology graph (nodes + links + partitions)
  * Sync policy (paused flag + blocklist)
  * Audit chain head (seq + last_hash)
  * Trust DB summary (top peers + scores)
  * Feature flags
  * Active distributed locks

Snapshots are written to ``data/cluster_snapshots/<ts>.json`` so an
operator can compare two points in time, or restore (read-only;
restore is *informational* — it doesn't push state back to the
cluster).

Use case: pre-upgrade checkpoint, post-incident forensic, capacity
planning baseline.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_SNAPSHOT_DIR = _DATA_DIR / "cluster_snapshots"


def _safe_call(fn) -> dict | None:
    try:
        return fn()
    except Exception as e:
        logger.debug("snapshot_section_failed", error=str(e)[:80])
        return {"error": str(e)[:120]}


def capture() -> dict:
    """Build a snapshot dict — pure read, no mutations."""
    started = time.time()
    payload: dict = {
        "captured_at": started,
        "version": 1,
    }

    # Services.
    payload["service_discovery"] = _safe_call(lambda: {
        "stats": __import__(
            "app.service_discovery.service_registry",
            fromlist=["get_registry"],
        ).get_registry().stats(),
    })

    # Topology graph stats.
    payload["topology"] = _safe_call(lambda: {
        "stats": __import__(
            "app.topology", fromlist=["get_topology_manager"],
        ).get_topology_manager().graph.stats(),
    })

    # Sync policy.
    payload["sync_policy"] = _safe_call(lambda: __import__(
        "app.services.sync_policy", fromlist=["get_sync_policy"],
    ).get_sync_policy().snapshot())

    # Audit chain.
    payload["audit_chain"] = _safe_call(lambda: __import__(
        "app.services.audit_replication", fromlist=["get_audit_replicator"],
    ).get_audit_replicator().head())

    # Trust top-N.
    payload["trust_top"] = _safe_call(lambda: {
        "rows": __import__(
            "app.services.trust_score", fromlist=["get_trust_db"],
        ).get_trust_db().list_top(limit=20),
    })

    # Feature flags.
    payload["feature_flags"] = _safe_call(lambda: __import__(
        "app.services.feature_flags", fromlist=["get_flag_store"],
    ).get_flag_store().snapshot())

    # Distributed locks (best-effort — list any known names).
    payload["locks"] = _safe_call(lambda: {
        "registry": __import__(
            "app.services.lock_priority_queue",
            fromlist=["get_lock_priority_queue"],
        ).get_lock_priority_queue().snapshot(),
    })

    # Partition state.
    payload["partition_state"] = _safe_call(lambda: __import__(
        "app.services.partition_detector", fromlist=["get_partition_state"],
    ).get_partition_state().snapshot())

    payload["captured_in_ms"] = round(
        (time.time() - started) * 1000.0, 2,
    )
    return payload


def save(snapshot: dict | None = None,
         *, label: str = "") -> Optional[Path]:
    """Persist the snapshot atomically. Returns the file path."""
    snap = snapshot if snapshot is not None else capture()
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(snap.get("captured_at") or time.time())
        suffix = f"-{label}" if label else ""
        path = _SNAPSHOT_DIR / f"{ts}{suffix}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception as e:
        logger.warning("snapshot_persist_failed", error=str(e))
        return None


def list_snapshots() -> list[dict]:
    if not _SNAPSHOT_DIR.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(_SNAPSHOT_DIR.iterdir()):
        if not p.is_file() or not p.name.endswith(".json"):
            continue
        try:
            stat = p.stat()
            out.append({
                "name":      p.name,
                "path":      str(p),
                "size_bytes": stat.st_size,
                "mtime":     stat.st_mtime,
            })
        except Exception:
            continue
    return out


def load(name: str) -> Optional[dict]:
    p = _SNAPSHOT_DIR / name
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def diff(name_a: str, name_b: str) -> dict:
    """Return the top-level keys that changed between two snapshots."""
    a = load(name_a)
    b = load(name_b)
    if a is None or b is None:
        return {"ok": False, "error": "not_found"}
    out: dict[str, dict] = {}
    for key in set(a.keys()) | set(b.keys()):
        va = a.get(key)
        vb = b.get(key)
        if va != vb:
            out[key] = {"changed": True}
    return {"ok": True, "changes": out}
