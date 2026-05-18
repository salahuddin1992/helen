"""
InstanceRegistry — Rendezvous instance heartbeat + roster.

Each rendezvous process registers itself in shared storage with an
`instance_id`, IP, port, current load, start time, and version. A background
heartbeat task refreshes the registration every `HEARTBEAT_INTERVAL_SEC`
(default 5s) with a TTL of `HEARTBEAT_TTL_SEC` (default 15s). If the process
dies the registration expires and the LB / admin endpoints see it disappear.

Roster reads use the underlying backend's `list_tunnels`-style SCAN under a
separate key namespace (`instance:` instead of `tunnel:`). This is done by
storing instances as ordinary signals with the dedicated namespace
`instance:<instance_id>` so every backend implementation works without new
methods.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from typing import Any, Optional

import structlog

from storage.backend import StorageBackend

logger = structlog.get_logger(__name__)


HEARTBEAT_INTERVAL_SEC = float(os.environ.get("HELEN_RENDEZVOUS_HEARTBEAT_INTERVAL", "5"))
HEARTBEAT_TTL_SEC = int(os.environ.get("HELEN_RENDEZVOUS_HEARTBEAT_TTL", "15"))
INSTANCE_KEY_NS = "instance"


def _gethostip() -> str:
    """Best-effort local IP discovery — falls back to hostname."""
    candidate = os.environ.get("HELEN_RENDEZVOUS_PUBLIC_IP")
    if candidate:
        return candidate
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


class InstanceRegistry:
    """Heartbeat-based roster of live Rendezvous instances.

    Stored in the same storage backend as everything else, under the
    `instance:` namespace. Uses the `register_signal` method so the
    implementation works on every backend transparently.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        instance_id: Optional[str] = None,
        public_ip: Optional[str] = None,
        port: int = 8080,
        version: str = "0.1.0",
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC,
        heartbeat_ttl: int = HEARTBEAT_TTL_SEC,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self._backend = backend
        self.instance_id = instance_id or os.environ.get(
            "HELEN_RENDEZVOUS_INSTANCE_ID"
        ) or f"rdv-{uuid.uuid4().hex[:10]}"
        self.public_ip = public_ip or _gethostip()
        self.port = port
        self.version = version
        self.started_at = time.time()
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_ttl = heartbeat_ttl
        self.extra = dict(extra or {})
        self._task: Optional[asyncio.Task[None]] = None
        self._load_provider: Optional[Any] = None
        self._stopped = asyncio.Event()

    def set_load_provider(self, provider: Any) -> None:
        """`provider` is a 0-arg callable returning a dict with load info."""
        self._load_provider = provider

    def _snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": self.instance_id,
            "public_ip": self.public_ip,
            "port": self.port,
            "version": self.version,
            "started_at": self.started_at,
            "heartbeat_at": time.time(),
            "uptime_sec": int(time.time() - self.started_at),
        }
        if self._load_provider is not None:
            try:
                payload["load"] = self._load_provider()
            except Exception as exc:  # pragma: no cover
                payload["load"] = {"error": str(exc)}
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload

    # ── Lifecycle ──────────────────────────────────────────

    async def start_heartbeat(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # Initial heartbeat: synchronous so the instance is registered
        # *before* `start_heartbeat` returns.
        await self._beat_once()
        self._task = asyncio.create_task(self._loop(), name=f"hb-{self.instance_id}")
        logger.info(
            "instance_registry_started",
            instance_id=self.instance_id,
            public_ip=self.public_ip,
            port=self.port,
            heartbeat_interval=self.heartbeat_interval,
        )

    async def stop_heartbeat(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # Best-effort deregister.
        try:
            await self._backend.delete_signal(f"{INSTANCE_KEY_NS}:{self.instance_id}")
        except Exception:  # pragma: no cover
            pass
        logger.info("instance_registry_stopped", instance_id=self.instance_id)

    async def _beat_once(self) -> None:
        try:
            await self._backend.register_signal(
                f"{INSTANCE_KEY_NS}:{self.instance_id}",
                self._snapshot(),
                self.heartbeat_ttl,
            )
        except Exception as exc:
            logger.warning("instance_heartbeat_failed", error=str(exc))

    async def _loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=self.heartbeat_interval,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                await self._beat_once()
        except asyncio.CancelledError:
            return

    # ── Reads ──────────────────────────────────────────────

    async def list_active_instances(self) -> list[dict[str, Any]]:
        """Roster snapshot. Always includes self if heartbeat has run."""
        prefix = f"{INSTANCE_KEY_NS}:"
        out: list[dict[str, Any]] = []
        try:
            keys = await self._backend.list_signals()
        except Exception:
            keys = []
        for key in keys:
            if not key.startswith(prefix):
                continue
            entry = await self._backend.lookup_signal(key)
            if entry is None:
                continue
            out.append(entry)
        out.sort(key=lambda e: e.get("instance_id", ""))
        return out

    async def count_active_instances(self) -> int:
        roster = await self.list_active_instances()
        return len(roster)
