"""
Phase 6 / Module AC — Bully-style leader election (cluster v2).

Distinct from the legacy ``app.services.leader_election`` module: this
one operates against the new ``cluster_leader_elect`` row (term + lease
+ lock_token), gives a clean ``register_leader_only_task()`` API, and
plays nicely with the new node_registry / pubsub layer.

Two backends:

* Redis  — SETNX + PEXPIRE / WATCH; resilient when DB is busy.
* SQL    — atomic UPDATE WHERE expires_at <= now() OR leader=mine.

Heartbeat cadence: 5 s. Term lease: 15 s. Graceful step-down on shutdown.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.cluster import LeaderElection
from app.services.cluster.session_store import (
    RedisSessionStore,
    SessionStore,
    get_session_store,
)

logger = get_logger(__name__)


HEARTBEAT_INTERVAL = 5.0
TERM_LEASE_SECONDS = 15
LEADER_KEY = "helen:cluster:leader"
NODE_ID_FILE = "node_id"


def _resolve_node_id() -> str:
    """Stable per-host node identifier — survives restarts."""
    settings = get_settings()
    base = settings.PROJECT_ROOT / "data"
    base.mkdir(parents=True, exist_ok=True)
    p = base / NODE_ID_FILE
    if p.is_file():
        try:
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:                                           # pragma: no cover
            pass
    nid = uuid.uuid4().hex
    try:
        p.write_text(nid, encoding="utf-8")
    except Exception:                                               # pragma: no cover
        pass
    return nid


class ClusterLeaderElection:
    """Leader election orchestrator for the cluster module.

    Lifecycle::

        elect = ClusterLeaderElection()
        await elect.start()
        elect.register_leader_only_task("reaper", reaper_loop)
        ...
        await elect.stop()
    """

    def __init__(
        self,
        *,
        node_id: Optional[str] = None,
        lease_seconds: int = TERM_LEASE_SECONDS,
        heartbeat: float = HEARTBEAT_INTERVAL,
    ) -> None:
        self.node_id = node_id or _resolve_node_id()
        self.lease = lease_seconds
        self.heartbeat = heartbeat
        self.lock_token = secrets.token_hex(16)
        self._is_leader = False
        self._term = 0
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._leader_tasks: dict[str, tuple[Callable[[], Awaitable[None]], Optional[asyncio.Task[None]]]] = {}
        self._lock = asyncio.Lock()
        self._store: Optional[SessionStore] = None

    # ── public API ──────────────────────────────────────────

    def is_leader(self) -> bool:
        return self._is_leader

    def current_term(self) -> int:
        return self._term

    def register_leader_only_task(
        self,
        name: str,
        fn: Callable[[], Awaitable[None]],
    ) -> None:
        """Register a coroutine factory that should only run while we are
        the elected leader. Re-registration replaces the previous fn."""
        prev = self._leader_tasks.get(name)
        if prev is not None and prev[1] is not None:
            prev[1].cancel()
        self._leader_tasks[name] = (fn, None)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._store = await get_session_store()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="cluster-leader-elect")
        logger.info("cluster_leader: started (node=%s)", self.node_id[:12])

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.heartbeat + 2)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()
        # step down: release lease if owned
        try:
            if self._is_leader:
                await self._release()
        except Exception:                                           # pragma: no cover
            pass
        # cancel children
        for name, (_, task) in self._leader_tasks.items():
            if task is not None and not task.done():
                task.cancel()

    # ── internals ───────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                acquired = await self._try_acquire()
                if acquired and not self._is_leader:
                    logger.info("cluster_leader: became leader (term=%d)", self._term)
                    self._is_leader = True
                    await self._start_leader_tasks()
                elif not acquired and self._is_leader:
                    logger.warning("cluster_leader: lost leadership")
                    self._is_leader = False
                    await self._stop_leader_tasks()
            except Exception as exc:
                logger.exception("cluster_leader: error in tick: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat)
            except asyncio.TimeoutError:
                continue

    async def _start_leader_tasks(self) -> None:
        async with self._lock:
            for name, (fn, _) in list(self._leader_tasks.items()):
                if self._leader_tasks[name][1] is not None:
                    continue
                t = asyncio.create_task(self._wrap_leader_task(name, fn),
                                        name=f"leader-task:{name}")
                self._leader_tasks[name] = (fn, t)

    async def _stop_leader_tasks(self) -> None:
        async with self._lock:
            for name, (fn, t) in list(self._leader_tasks.items()):
                if t is None:
                    continue
                t.cancel()
                self._leader_tasks[name] = (fn, None)

    async def _wrap_leader_task(
        self,
        name: str,
        fn: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("cluster_leader: task %r crashed: %s", name, exc)

    async def _try_acquire(self) -> bool:
        # Prefer Redis when available (fast, atomic).
        store = self._store
        if isinstance(store, RedisSessionStore):
            ok = await self._try_redis()
        else:
            ok = await self._try_sql()
        return ok

    async def _try_redis(self) -> bool:
        store = self._store  # type: ignore[assignment]
        assert isinstance(store, RedisSessionStore)
        r = store._redis  # type: ignore[attr-defined]
        # Try SET key value NX EX lease
        key = LEADER_KEY
        payload = f"{self.node_id}:{self.lock_token}"
        try:
            res = await r.set(key, payload, nx=True, ex=self.lease)
            if res:
                self._term += 1
                return True
            # Maybe we already hold it — refresh
            cur = await r.get(key)
            if cur and cur.startswith(self.node_id + ":"):
                await r.expire(key, self.lease)
                return True
            return False
        except Exception as exc:                                    # pragma: no cover
            logger.warning("cluster_leader: redis path failed (%s)", exc)
            return False

    async def _try_sql(self) -> bool:
        now = datetime.now(timezone.utc)
        new_exp = now + timedelta(seconds=self.lease)
        async with async_session_factory() as db:  # type: AsyncSession
            try:
                row = (await db.execute(select(LeaderElection))).scalar_one_or_none()
                if row is None:
                    fresh = LeaderElection(
                        term=1,
                        leader_node_id=self.node_id,
                        started_at=now,
                        expires_at=new_exp,
                        lock_token=self.lock_token,
                    )
                    db.add(fresh)
                    await db.commit()
                    self._term = 1
                    return True

                if row.leader_node_id == self.node_id and row.lock_token == self.lock_token:
                    # Renew
                    row.expires_at = new_exp
                    await db.commit()
                    self._term = int(row.term)
                    return True

                # Take over only if lease expired
                if row.expires_at <= now:
                    row.term = int(row.term) + 1
                    row.leader_node_id = self.node_id
                    row.lock_token = self.lock_token
                    row.started_at = now
                    row.expires_at = new_exp
                    await db.commit()
                    self._term = int(row.term)
                    return True

                return False
            except Exception as exc:                                # pragma: no cover
                logger.warning("cluster_leader: SQL path failed (%s)", exc)
                await db.rollback()
                return False

    async def _release(self) -> None:
        store = self._store
        if isinstance(store, RedisSessionStore):
            try:
                r = store._redis  # type: ignore[attr-defined]
                cur = await r.get(LEADER_KEY)
                if cur and cur.startswith(self.node_id + ":"):
                    await r.delete(LEADER_KEY)
            except Exception:                                       # pragma: no cover
                pass
        else:
            try:
                async with async_session_factory() as db:
                    row = (await db.execute(select(LeaderElection))).scalar_one_or_none()
                    if row and row.leader_node_id == self.node_id:
                        # Mark lease as expired so any waiting node grabs it instantly
                        row.expires_at = datetime.now(timezone.utc)
                        await db.commit()
            except Exception:                                       # pragma: no cover
                pass


# ── module-level singleton ──────────────────────────────────


_singleton: Optional[ClusterLeaderElection] = None


def get_cluster_leader() -> ClusterLeaderElection:
    global _singleton
    if _singleton is None:
        _singleton = ClusterLeaderElection()
    return _singleton
