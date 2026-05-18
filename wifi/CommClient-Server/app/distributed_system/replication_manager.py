"""Replication manager — facade over services.replication_manager.

The lower-level ``services.replication_manager`` is a fire-and-forget
K-replica store. This file exposes:

  * ``put_replicated`` — write + return the resolved record.
  * ``get_replicated`` — local read.
  * ``stats``          — count of locally-stored records.

Future evolution can add proper read-repair / consistent-read paths
without changing the public surface.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.distributed_system.distributed_config import get_config
from app.distributed_system.distributed_events import emit
from app.distributed_system.distributed_exceptions import ReplicationError

logger = get_logger(__name__)


def put_replicated(kind: str, key: str, value: Any,
                   *, k: Optional[int] = None) -> dict:
    cfg = get_config()
    try:
        from app.services.replication_manager import put as _put
    except ImportError as e:
        raise ReplicationError(f"replication primitive missing: {e}")
    rec = _put(kind, key, value, k=k or cfg.replication_factor)
    emit("replication.put", {
        "kind": kind, "key": key, "version": rec.get("version"),
    })
    return rec


def get_replicated(kind: str, key: str) -> Optional[dict]:
    try:
        from app.services.replication_manager import get as _get
    except ImportError as e:
        raise ReplicationError(f"replication primitive missing: {e}")
    return _get(kind, key)


def stats() -> dict:
    try:
        from app.services.replication_manager import _store
        rows = _store().all_keys()
        return {"local_records": len(rows)}
    except Exception:
        return {"local_records": 0}
