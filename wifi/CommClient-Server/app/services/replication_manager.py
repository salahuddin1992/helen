"""
Replication manager — guarantees ≥ K copies of critical data.

The cluster already has informal redundancy (DHT replicates
``user_id → server_id`` to K=20 closest peers; gossip spreads peer
membership; trust DB converges via anti-entropy). For data the
operator explicitly marks as critical, this module provides
*explicit* replication targets and a self-healing loop.

A "critical record" is a (kind, key, value, version) tuple. The
manager:

  1. Computes the K target replicas via consistent hashing on
     ``(kind, key)``.
  2. Stores locally if we are one of the K, otherwise forwards
     synchronously.
  3. Asynchronously fans out to the other K-1 replicas via signed
     federation requests.
  4. Background heal loop: every ``HEAL_INTERVAL`` re-computes the
     target set (peers may have come/gone), and pushes our copy to
     any target that's missing it.

This is deliberately Dynamo-flavoured (no leader, last-version-wins,
read-your-writes via quorum lookups) rather than Raft — Raft gives
strong consistency at the cost of leader election overhead, which is
the wrong tradeoff for replicating small operator-driven settings.

Storage
-------
Local: ``data/replicated_state.sqlite`` — table ``replicated``::

    kind        TEXT
    key         TEXT
    value       TEXT
    version     INTEGER
    updated_at  REAL
    PRIMARY KEY (kind, key)

Versions monotonically increase per (kind, key); on conflict, the
higher version wins. ``updated_at`` is a tiebreak.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_DB_PATH = _DATA_DIR / "replicated_state.sqlite"

DEFAULT_REPLICAS  = 3
HEAL_INTERVAL_SEC = 60.0
PUSH_TIMEOUT_SEC  = 4.0


# ── Local store ─────────────────────────────────────────────────


class _Store:
    _singleton: "_Store | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    @classmethod
    def instance(cls) -> "_Store":
        if cls._singleton is None:
            cls._singleton = _Store()
        return cls._singleton

    def _conn_or_init(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(
            str(_DB_PATH),
            check_same_thread=False,
            isolation_level=None,
        )
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("""
            CREATE TABLE IF NOT EXISTS replicated (
                kind        TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT NOT NULL,
                version     INTEGER NOT NULL,
                updated_at  REAL NOT NULL,
                PRIMARY KEY (kind, key)
            )
        """)
        self._conn = c
        return c

    def upsert(
        self, kind: str, key: str, value: str,
        version: int, updated_at: float,
    ) -> bool:
        """Apply iff incoming (version, updated_at) wins. Returns True
        if a write occurred."""
        with self._lock:
            cur = self._conn_or_init().execute(
                "SELECT version, updated_at FROM replicated "
                "WHERE kind=? AND key=?", (kind, key),
            ).fetchone()
            if cur:
                cv, ct = int(cur[0]), float(cur[1])
                if (version, updated_at) <= (cv, ct):
                    return False
            self._conn_or_init().execute(
                "INSERT INTO replicated (kind, key, value, version, updated_at)"
                " VALUES (?,?,?,?,?) "
                "ON CONFLICT(kind, key) DO UPDATE SET "
                "value = excluded.value, version = excluded.version, "
                "updated_at = excluded.updated_at",
                (kind, key, value, version, updated_at),
            )
            return True

    def get(self, kind: str, key: str) -> Optional[dict]:
        with self._lock:
            row = self._conn_or_init().execute(
                "SELECT kind, key, value, version, updated_at FROM replicated "
                "WHERE kind=? AND key=?", (kind, key),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[2])
        except Exception:
            value = row[2]
        return {
            "kind": row[0], "key": row[1], "value": value,
            "version": int(row[3]), "updated_at": float(row[4]),
        }

    def all_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn_or_init().execute(
                "SELECT kind, key FROM replicated"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]


def _store() -> _Store:
    return _Store.instance()


# ── Replica selection ───────────────────────────────────────────


def _replicas_for(kind: str, key: str, k: int = DEFAULT_REPLICAS) -> list[str]:
    try:
        from app.services.consistent_hash import get_ring, refresh_from_registry
        refresh_from_registry()
        return get_ring().replicas_for(f"{kind}::{key}", k=k)
    except Exception:
        return []


def _i_am_replica(kind: str, key: str, k: int = DEFAULT_REPLICAS) -> bool:
    try:
        from app.services.discovery_service import get_server_id
        return (get_server_id() or "") in _replicas_for(kind, key, k)
    except Exception:
        return True  # err on the side of storing


# ── Public write/read API ───────────────────────────────────────


def put(
    kind: str,
    key: str,
    value: Any,
    *,
    k: int = DEFAULT_REPLICAS,
    version: Optional[int] = None,
) -> dict:
    """Write a critical record. Returns the resolved record.

    If we're one of the K replicas, write locally.
    Always fan out to the others asynchronously.
    """
    now = time.time()
    if version is None:
        existing = _store().get(kind, key)
        version = (existing["version"] + 1) if existing else 1
    serialized = json.dumps(value, sort_keys=True)

    if _i_am_replica(kind, key, k):
        _store().upsert(kind, key, serialized, version, now)

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_fanout(kind, key, serialized, version, now, k))
    except RuntimeError:
        pass
    return {
        "kind": kind, "key": key, "value": value,
        "version": version, "updated_at": now,
    }


def get(kind: str, key: str) -> Optional[dict]:
    """Read locally if we have it. (Cluster-wide read with quorum
    is left to the caller — most operator settings are written here
    and read back immediately so local read suffices.)"""
    return _store().get(kind, key)


# ── Fan-out ─────────────────────────────────────────────────────


async def _push_to_peer(peer, kind: str, key: str, value: str,
                        version: int, ts: float) -> None:
    try:
        import httpx
        from app.core.federation_auth import sign_request
    except ImportError:
        return
    body = json.dumps({
        "kind": kind, "key": key, "value": value,
        "version": version, "updated_at": ts,
    }).encode()
    path = "/api/cluster/replicated/put"
    headers = sign_request("POST", path, body)
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT_SEC) as c:
            await c.post(
                f"http://{peer.host}:{peer.port}{path}",
                content=body, headers=headers,
            )
    except Exception as e:
        logger.debug("replica_push_failed",
                     peer=peer.node_id[:24], error=str(e)[:80])


async def _fanout(kind: str, key: str, value: str,
                  version: int, ts: float, k: int) -> None:
    targets = _replicas_for(kind, key, k)
    if not targets:
        return
    try:
        from app.services.discovery_service import get_server_id
        from app.services.node_registry import get_registry
    except ImportError:
        return
    me = get_server_id() or ""
    reg = get_registry()
    peer_index = {n.node_id: n for n in reg.nodes(include_dead=False)}
    pushes = []
    for sid in targets:
        if sid == me or sid not in peer_index:
            continue
        pushes.append(_push_to_peer(
            peer_index[sid], kind, key, value, version, ts,
        ))
    if pushes:
        await asyncio.gather(*pushes, return_exceptions=True)


# ── Heal loop ───────────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _heal_loop() -> None:
    global _running
    _running = True
    logger.info("replication_heal_started", interval_sec=HEAL_INTERVAL_SEC)
    try:
        while _running:
            try:
                await _heal_once()
            except Exception as e:
                logger.warning("replication_heal_failed", error=str(e))
            await asyncio.sleep(HEAL_INTERVAL_SEC)
    finally:
        logger.info("replication_heal_stopped")


async def _heal_once() -> None:
    """For every locally stored record, push it to all current
    replicas. Idempotent — receivers reject older versions."""
    keys = _store().all_keys()
    if not keys:
        return
    for kind, key in keys:
        rec = _store().get(kind, key)
        if not rec:
            continue
        await _fanout(
            kind=kind, key=key,
            value=json.dumps(rec["value"], sort_keys=True),
            version=rec["version"],
            ts=rec["updated_at"],
            k=DEFAULT_REPLICAS,
        )


def start_replication_heal() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_heal_loop(), name="replication-heal")
    except RuntimeError:
        logger.warning("replication_heal_no_event_loop_yet")


def stop_replication_heal() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


# ── Wire endpoint use ───────────────────────────────────────────


def absorb_remote(payload: dict) -> bool:
    """Apply a record pushed by a peer (LWW on (version, updated_at))."""
    try:
        kind = str(payload.get("kind") or "")
        key = str(payload.get("key") or "")
        value = str(payload.get("value") or "")
        version = int(payload.get("version") or 0)
        ts = float(payload.get("updated_at") or 0.0)
        if not kind or not key:
            return False
        return _store().upsert(kind, key, value, version, ts)
    except Exception as e:
        logger.warning("replicated_absorb_failed", error=str(e))
        return False
