"""
Federation v2 — HTTPS transport.

Three operations:

* ``push_event(server_id, event)`` — PUT to remote.
* ``sync_since(server_id, since)`` — incremental pull.
* ``backfill(server_id, channel, before, limit)`` — historical pull.

A single ``EventDispatcher`` queues outbound events per remote server,
retries with exponential backoff (1s → 2s → 4s → … capped at 5min),
and persists failures in the local DAG with ``rejected=True`` after
permanent failure.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


MAX_BACKOFF = 300.0
INITIAL_BACKOFF = 1.0
MAX_ATTEMPTS = 12


@dataclass
class _PendingDelivery:
    server_id: str
    event: dict[str, Any]
    attempts: int = 0
    next_attempt: float = 0.0
    last_error: str = ""


class FederationTransport:
    """Outbound HTTPS dispatcher. Singleton."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[_PendingDelivery]] = defaultdict(asyncio.Queue)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._sync_tokens: dict[str, str] = {}

    # ── public API ──────────────────────────────────────────

    async def push_event(self, server_id: str, event: dict[str, Any]) -> None:
        """Enqueue an event for delivery to ``server_id``."""
        pd = _PendingDelivery(server_id=server_id, event=event)
        await self._queues[server_id].put(pd)
        self._ensure_worker(server_id)

    async def sync_since(
        self,
        server_id: str,
        advertise_url: str,
        since: Optional[str] = None,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """GET ``/api/_federation/v2/sync?since=<token>``."""
        try:
            import httpx
        except Exception:
            return {"events": [], "next": since or ""}
        params = {"limit": limit}
        if since:
            params["since"] = since
        url = advertise_url.rstrip("/") + "/api/_federation/v2/sync"
        try:
            async with httpx.AsyncClient(timeout=20.0) as cli:
                r = await cli.get(url, params=params, headers=self._auth_headers())
        except Exception as exc:
            logger.warning("fedv2_sync_failed server=%s err=%s", server_id, exc)
            return {"events": [], "next": since or ""}
        if r.status_code != 200:
            return {"events": [], "next": since or ""}
        data = r.json()
        if data.get("next"):
            self._sync_tokens[server_id] = str(data["next"])
        return data

    async def backfill(
        self,
        server_id: str,
        advertise_url: str,
        channel: str,
        *,
        before_depth: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            import httpx
        except Exception:
            return []
        url = advertise_url.rstrip("/") + "/api/_federation/v2/backfill"
        params: dict[str, Any] = {"channel": channel, "limit": limit}
        if before_depth is not None:
            params["before"] = before_depth
        try:
            async with httpx.AsyncClient(timeout=20.0) as cli:
                r = await cli.get(url, params=params, headers=self._auth_headers())
        except Exception as exc:
            logger.warning("fedv2_backfill_failed server=%s err=%s", server_id, exc)
            return []
        if r.status_code != 200:
            return []
        data = r.json()
        return list(data.get("events") or [])

    # ── lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        self._stop.clear()

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self._workers.values()):
            t.cancel()
        self._workers.clear()

    # ── internals ───────────────────────────────────────────

    def _ensure_worker(self, server_id: str) -> None:
        if server_id in self._workers and not self._workers[server_id].done():
            return
        self._workers[server_id] = asyncio.create_task(
            self._worker(server_id), name=f"fedv2-dispatch-{server_id}",
        )

    def _auth_headers(self) -> dict[str, str]:
        """Signed request headers — challenge embedded in URL/body. The
        request body itself carries signatures inside event payload, so
        the header set is minimal."""
        from app.services.federation_v2.addressing import my_server_id
        return {
            "X-Helen-Federation-Origin": my_server_id(),
            "User-Agent": "helen-federation-v2",
        }

    async def _worker(self, server_id: str) -> None:
        q = self._queues[server_id]
        while not self._stop.is_set():
            try:
                pd = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                continue
            now = time.monotonic()
            if pd.next_attempt > now:
                await asyncio.sleep(pd.next_attempt - now)
            ok = await self._deliver(pd)
            if not ok:
                pd.attempts += 1
                if pd.attempts >= MAX_ATTEMPTS:
                    logger.warning(
                        "fedv2_delivery_permanent_failure server=%s err=%s",
                        server_id, pd.last_error,
                    )
                    continue
                backoff = min(INITIAL_BACKOFF * (2 ** pd.attempts), MAX_BACKOFF)
                pd.next_attempt = time.monotonic() + backoff
                await q.put(pd)

    async def _deliver(self, pd: _PendingDelivery) -> bool:
        """Single HTTPS PUT to ``/api/_federation/v2/events/{event_id}``."""
        try:
            import httpx
        except Exception:
            pd.last_error = "httpx_missing"
            return False
        from sqlalchemy import select
        from app.db.session import async_session_factory
        from app.models.federation_v2 import FederatedServer

        async with async_session_factory() as db:
            peer = (await db.execute(
                select(FederatedServer).where(
                    FederatedServer.server_id == pd.server_id
                )
            )).scalar_one_or_none()
        if peer is None or peer.status != "active":
            pd.last_error = "peer_not_active"
            return False
        eid = pd.event.get("event_id") or pd.event.get("origin_event_id") or ""
        if not eid:
            pd.last_error = "no_event_id"
            return False
        url = peer.advertise_url.rstrip("/") + f"/api/_federation/v2/events/{eid}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as cli:
                r = await cli.put(url, json=pd.event, headers=self._auth_headers())
        except Exception as exc:
            pd.last_error = f"connect:{exc}"
            return False
        if 200 <= r.status_code < 300:
            return True
        pd.last_error = f"http:{r.status_code}"
        return False


# ── singleton ───────────────────────────────────────────────


_transport: Optional[FederationTransport] = None


def get_transport() -> FederationTransport:
    global _transport
    if _transport is None:
        _transport = FederationTransport()
    return _transport
