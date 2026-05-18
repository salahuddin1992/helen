"""
Server registry — distributed graph of known Helen servers.

This is the foundation that ``route_planner`` reads from to compute
routes (shortest path, region-aware, congestion-aware). Without a
shared registry, each server only knows about peers it has discovered
via UDP broadcast / DHT — fine for ad-hoc LAN, but inadequate for
multi-region production.

Storage layout
--------------
    KEY  helen:registry:server:{server_id}      HASH  {region, capacity, version, ...}
                                                TTL   45 seconds (renewed every 15s)
    SET  helen:registry:servers                 SET   of all known server_ids
    KEY  helen:registry:server:{server_id}:load HASH  load metrics snapshot
                                                TTL   30 seconds

API
---
    >>> svc = ServerRegistryService(redis_client, this_server_id="server_001",
    ...                             region="us-east-1", capacity={"max_calls": 500})
    >>> await svc.register()
    >>> await svc.heartbeat_loop_start()
    >>> servers = await svc.list_all_healthy()
    >>> await svc.publish_load(load_snapshot)
    >>> peer_load = await svc.get_load("server_037")
    >>> await svc.mark_unhealthy("server_047", reason="circuit_open")

Falls back to in-process when Redis is None — only this server's own
record is visible, but the API stays consistent for callers.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

REGISTRY_TTL_SEC = 45
HEARTBEAT_INTERVAL_SEC = 15
LOAD_TTL_SEC = 30
HEALTHY_THRESHOLD_SEC = 30  # last heartbeat within this window = healthy


@dataclass
class ServerRecord:
    server_id: str
    region: str
    version: str
    capacity_max_calls: int
    capacity_max_users: int
    last_heartbeat_unix: float
    started_at_unix: float
    sfu_available: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def age_sec(self) -> float:
        return time.time() - self.last_heartbeat_unix

    @property
    def is_stale(self) -> bool:
        return self.age_sec > HEALTHY_THRESHOLD_SEC

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ServerRecord":
        # Tolerate extra unknown fields (forward compat).
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class LoadSnapshot:
    """Lightweight projection of load_monitor output that the
    registry service distributes to peers. Heavy details (per-queue
    depth, etc.) stay local; we share enough for routing decisions.
    """
    server_id: str
    timestamp: float
    cpu_percent: float
    memory_percent: float
    event_loop_lag_ms: float
    active_sockets: int
    active_calls: int
    queue_depth_p0: int
    queue_depth_p1: int
    health_score: float  # 0.0–1.0, derived

    def to_dict(self) -> dict:
        return asdict(self)


class ServerRegistryService:
    def __init__(
        self,
        redis_client=None,
        this_server_id: str = "local",
        region: str = "default",
        version: str = "unknown",
        capacity_max_calls: int = 500,
        capacity_max_users: int = 5000,
        sfu_available: bool = False,
    ) -> None:
        self._redis = redis_client
        self._sid = this_server_id
        self._region = region
        self._version = version
        self._capacity_max_calls = capacity_max_calls
        self._capacity_max_users = capacity_max_users
        self._sfu_available = sfu_available
        self._started_at = time.time()
        # In-process fallback. Maps server_id → ServerRecord.
        self._local: dict[str, ServerRecord] = {}
        self._local_loads: dict[str, LoadSnapshot] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    @property
    def is_distributed(self) -> bool:
        return self._redis is not None

    @property
    def server_id(self) -> str:
        return self._sid

    # ── Registration / heartbeat ───────────────────────────────

    async def register(self) -> None:
        """Insert/refresh our own record and add our id to the
        servers set."""
        rec = ServerRecord(
            server_id=self._sid,
            region=self._region,
            version=self._version,
            capacity_max_calls=self._capacity_max_calls,
            capacity_max_users=self._capacity_max_users,
            last_heartbeat_unix=time.time(),
            started_at_unix=self._started_at,
            sfu_available=self._sfu_available,
        )
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as p:
                    p.setex(
                        f"helen:registry:server:{self._sid}",
                        REGISTRY_TTL_SEC,
                        json.dumps(rec.to_dict()),
                    )
                    p.sadd("helen:registry:servers", self._sid)
                    p.expire("helen:registry:servers", REGISTRY_TTL_SEC * 4)
                    await p.execute()
                return
            except Exception as e:
                logger.warning("registry_register_failed", error=str(e))

        self._local[self._sid] = rec

    async def heartbeat_loop_start(self) -> None:
        """Start a background task that re-registers every
        HEARTBEAT_INTERVAL_SEC. Idempotent."""
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await self.register()
                except Exception as e:
                    logger.warning("registry_heartbeat_failed", error=str(e))
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=HEARTBEAT_INTERVAL_SEC,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        self._stopped.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, BaseException):
                pass
            self._heartbeat_task = None

    async def deregister(self) -> None:
        """Voluntarily leave the registry — clean shutdown only.
        Death detection still relies on TTL expiry; this is just
        being polite."""
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as p:
                    p.delete(f"helen:registry:server:{self._sid}")
                    p.srem("helen:registry:servers", self._sid)
                    await p.execute()
            except Exception as e:
                logger.warning("registry_deregister_failed", error=str(e))
        self._local.pop(self._sid, None)

    # ── Read API ───────────────────────────────────────────────

    async def get(self, server_id: str) -> Optional[ServerRecord]:
        if self._redis is not None:
            try:
                v = await self._redis.get(f"helen:registry:server:{server_id}")
                if v is None:
                    return None
                if isinstance(v, bytes):
                    v = v.decode("utf-8")
                return ServerRecord.from_dict(json.loads(v))
            except Exception as e:
                logger.warning("registry_get_failed", server_id=server_id, error=str(e))
                return None
        return self._local.get(server_id)

    async def list_all(self) -> list[ServerRecord]:
        if self._redis is not None:
            try:
                ids = await self._redis.smembers("helen:registry:servers")
                ids = [i.decode("utf-8") if isinstance(i, bytes) else i for i in ids]
            except Exception as e:
                logger.warning("registry_list_failed", error=str(e))
                return []
            out = []
            for sid in ids:
                rec = await self.get(sid)
                if rec is not None:
                    out.append(rec)
            return out
        return list(self._local.values())

    async def list_all_healthy(self) -> list[ServerRecord]:
        all_servers = await self.list_all()
        return [s for s in all_servers if not s.is_stale]

    async def list_in_region(self, region: str) -> list[ServerRecord]:
        return [s for s in await self.list_all_healthy() if s.region == region]

    async def list_with_sfu(self) -> list[ServerRecord]:
        return [s for s in await self.list_all_healthy() if s.sfu_available]

    # ── Load metrics distribution ──────────────────────────────

    async def publish_load(self, snapshot: LoadSnapshot) -> None:
        """Distribute our current load snapshot. Called every 5s by
        load_monitor."""
        if self._redis is not None:
            try:
                await self._redis.setex(
                    f"helen:registry:server:{snapshot.server_id}:load",
                    LOAD_TTL_SEC,
                    json.dumps(snapshot.to_dict()),
                )
                return
            except Exception as e:
                logger.warning("registry_publish_load_failed", error=str(e))
        self._local_loads[snapshot.server_id] = snapshot

    async def get_load(self, server_id: str) -> Optional[LoadSnapshot]:
        if self._redis is not None:
            try:
                v = await self._redis.get(
                    f"helen:registry:server:{server_id}:load",
                )
                if v is None:
                    return None
                if isinstance(v, bytes):
                    v = v.decode("utf-8")
                return LoadSnapshot(**json.loads(v))
            except Exception as e:
                logger.warning("registry_get_load_failed", error=str(e))
                return None
        return self._local_loads.get(server_id)

    async def all_loads(self) -> dict[str, LoadSnapshot]:
        out: dict[str, LoadSnapshot] = {}
        for s in await self.list_all_healthy():
            load = await self.get_load(s.server_id)
            if load is not None:
                out[s.server_id] = load
        return out

    # ── Admission control + capacity-aware routing ─────────────
    #
    # The "unlimited capacity" promise depends on two things:
    #   1. Each server refuses NEW work when its own load is over a
    #      headroom threshold (so existing calls don't degrade).
    #   2. The federation gateway picks the LEAST-loaded peer for
    #      that work instead of crashing the loaded server.
    #
    # We compose a normalised health_score 0..1 from the LoadSnapshot
    # so heterogeneous metrics (CPU%, mem%, queue depth, call slots)
    # all collapse into a single "how saturated is this box".

    @staticmethod
    def _score_for_load(rec: "ServerRecord", load: Optional[LoadSnapshot]) -> float:
        """Higher score = MORE LOAD (worse for new work).
        Returns ``inf`` when the server is unhealthy."""
        if rec.is_stale:
            return float("inf")
        if load is None:
            # No load report yet — neutral default that still picks
            # this server over a known-saturated one.
            return 0.5
        cpu = max(0.0, min(100.0, load.cpu_percent)) / 100.0
        mem = max(0.0, min(100.0, load.memory_percent)) / 100.0
        # Capacity utilisation — call slot consumption is a hard
        # cap; once it crosses 1.0 we're over committed.
        slots = (
            load.active_calls / rec.capacity_max_calls
            if rec.capacity_max_calls > 0 else 0.0
        )
        users = (
            load.active_sockets / rec.capacity_max_users
            if rec.capacity_max_users > 0 else 0.0
        )
        # Weighted blend — slots dominate because that's the resource
        # we'll actually run out of first; memory is next; CPU is a
        # transient signal that recovers between calls.
        return (
            0.40 * slots
            + 0.20 * users
            + 0.20 * mem
            + 0.15 * cpu
            + 0.05 * min(1.0, load.event_loop_lag_ms / 250.0)
        )

    async def can_accept_new_call(self, headroom: float = 0.85) -> bool:
        """Should this server admit a brand-new call?

        Returns False when our current load score is over the headroom
        threshold (default 85%). The caller should reject the call and
        pass the client a redirect to ``find_least_loaded_peer()``.
        """
        rec = await self.get(self._sid)
        load = await self.get_load(self._sid)
        if not rec:
            return True   # registry not yet populated — admit
        score = self._score_for_load(rec, load)
        return score < headroom

    async def find_least_loaded_peer(
        self,
        *,
        require_sfu: bool = False,
        prefer_region: Optional[str] = None,
    ) -> Optional[ServerRecord]:
        """Pick the healthiest peer (lowest load score) for a redirect.

        ``require_sfu`` filters to servers that advertise SFU
        availability — used when admitting a call that's already
        large enough to need an SFU.
        ``prefer_region`` is a soft preference: if any peer in that
        region is below the headroom threshold, return it; else fall
        back to the global minimum.
        """
        candidates = await self.list_all_healthy()
        candidates = [c for c in candidates if c.server_id != self._sid]
        if require_sfu:
            candidates = [c for c in candidates if c.sfu_available]
        if not candidates:
            return None

        loads = await self.all_loads()
        scored = [
            (self._score_for_load(c, loads.get(c.server_id)), c)
            for c in candidates
        ]
        # Soft regional preference.
        if prefer_region:
            in_region = [t for t in scored if t[1].region == prefer_region]
            if in_region:
                in_region.sort(key=lambda t: t[0])
                if in_region[0][0] < float("inf"):
                    return in_region[0][1]
        scored.sort(key=lambda t: t[0])
        if scored and scored[0][0] < float("inf"):
            return scored[0][1]
        return None

    # ── Health labeling ────────────────────────────────────────

    async def find_unhealthy(self, threshold_sec: int = HEALTHY_THRESHOLD_SEC) -> list[str]:
        """Return ids of servers whose last heartbeat is older than
        threshold. Used by death-sweeper to trigger origin
        re-election."""
        all_servers = await self.list_all()
        now = time.time()
        return [
            s.server_id for s in all_servers
            if (now - s.last_heartbeat_unix) > threshold_sec
        ]

    async def mark_unhealthy(self, server_id: str, reason: str = "") -> None:
        """Explicitly evict a peer (e.g. circuit breaker tripped). The
        TTL would catch it eventually, but this gives us immediate
        consistency for routing."""
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as p:
                    p.delete(f"helen:registry:server:{server_id}")
                    p.srem("helen:registry:servers", server_id)
                    await p.execute()
                logger.info("registry_marked_unhealthy", server_id=server_id, reason=reason)
                return
            except Exception as e:
                logger.warning("registry_mark_unhealthy_failed", error=str(e))
        self._local.pop(server_id, None)


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[ServerRegistryService] = None


def get_registry_service() -> ServerRegistryService:
    global _svc
    if _svc is None:
        _svc = ServerRegistryService(redis_client=None)
    return _svc


def configure(
    *,
    redis_client,
    this_server_id: str,
    region: str = "default",
    version: str = "unknown",
    capacity_max_calls: int = 500,
    capacity_max_users: int = 5000,
    sfu_available: bool = False,
) -> ServerRegistryService:
    global _svc
    _svc = ServerRegistryService(
        redis_client=redis_client,
        this_server_id=this_server_id,
        region=region,
        version=version,
        capacity_max_calls=capacity_max_calls,
        capacity_max_users=capacity_max_users,
        sfu_available=sfu_available,
    )
    logger.info(
        "server_registry_service_configured",
        mode="redis" if redis_client is not None else "in-process",
        server_id=this_server_id,
        region=region,
    )
    return _svc
