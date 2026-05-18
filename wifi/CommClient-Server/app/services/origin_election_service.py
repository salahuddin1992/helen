"""
Origin election — leader lease for active calls.

Today every ``ActiveCall`` is origin-pinned to whichever server
created it. Origin death = call freeze for ~90s until orphan sweep.
This service replaces that with explicit leader lease semantics: a
server claims an origin lease for ``call_id``, renews while it's
healthy, and a sweeper re-elects from a healthy participant if the
incumbent dies.

Flow
----
::

    on call_create(call_id):
        if claim_origin(call_id) -> True:
            # Become the authoritative origin. Run a renewal loop.
        else:
            # Someone else got there first. Forward to them via RPC.

    on participant heartbeat:
        # If we're the origin, our renewal_loop is already running.
        # Otherwise we just forward heartbeats.

    on death sweep (server_X dead):
        for call_id in active_calls owned by server_X:
            new_owner = pick_healthy_participant_server(call_id)
            if new_owner:
                trigger re-election: claim_origin from new_owner
                broadcast call.origin_changed event

API
---
    >>> svc = OriginElectionService(lock_service, registry_service,
    ...                              this_server_id="server_001")
    >>> claimed = await svc.claim_origin("call_abc")
    >>> if claimed:
    ...     # we're the origin; lease auto-renews in background
    ...     await svc.release("call_abc")  # on call end
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

# Lease duration. Half this is the renewal interval. Tune carefully:
# too short = renewal storms; too long = slow re-election on death.
LEASE_TTL_SEC = 30
LEASE_RENEW_EVERY_SEC = 10
DEAD_SERVER_THRESHOLD_SEC = 45


class OriginElectionService:
    def __init__(
        self,
        lock_service,
        registry_service,
        this_server_id: str = "local",
    ) -> None:
        self._lock = lock_service
        self._registry = registry_service
        self._sid = this_server_id
        # Track held leases. Maps call_id → _LeaseHolder. Release sets
        # the entry to None so we can detect lease loss vs voluntary
        # release.
        self._holders: dict[str, "_LeaseHolder"] = {}
        # Sweeper task — runs once configured.
        self._sweeper_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        # Hooks. Set via on_origin_changed / on_lease_lost so
        # call_handlers can react.
        self._on_origin_changed = None  # type: Optional[callable]
        self._on_lease_lost = None      # type: Optional[callable]

    # ── Hooks ──────────────────────────────────────────────────

    def on_origin_changed(self, handler) -> None:
        """``handler(call_id: str, new_origin: str) -> Awaitable``.
        Called when this server has just claimed origin (after either
        new-call create or re-election from a dead origin)."""
        self._on_origin_changed = handler

    def on_lease_lost(self, handler) -> None:
        """``handler(call_id: str) -> Awaitable``.
        Called when our renewal loop discovers we no longer hold the
        lease — usually means another server took over (e.g. our
        process was paused too long). Caller should drop the call from
        local in-memory state."""
        self._on_lease_lost = handler

    # ── Acquisition ────────────────────────────────────────────

    async def claim_origin(self, call_id: str) -> bool:
        """Try to claim origin for ``call_id``. Returns True on
        success (this server is now the authoritative origin and a
        renewal loop is running). Returns False if another server
        already holds it."""
        if call_id in self._holders and self._holders[call_id].active:
            return True  # already ours

        held = await self._lock.hold(
            f"call:origin:{call_id}",
            ttl_seconds=LEASE_TTL_SEC,
            renew_every=LEASE_RENEW_EVERY_SEC,
        )
        if not held.acquired:
            return False

        holder = _LeaseHolder(self, call_id, held)
        self._holders[call_id] = holder
        # Watch for renewal failure so we can fire on_lease_lost.
        asyncio.create_task(holder.watch())

        if self._on_origin_changed is not None:
            try:
                await self._on_origin_changed(call_id, self._sid)
            except Exception as e:
                logger.warning("on_origin_changed_handler_failed",
                               call_id=call_id, error=str(e))

        logger.info("origin_claimed", call_id=call_id, server_id=self._sid)
        return True

    async def is_origin_for(self, call_id: str) -> bool:
        h = self._holders.get(call_id)
        return h is not None and h.active

    async def get_origin(self, call_id: str) -> Optional[str]:
        """Inspect Redis directly for the current lease holder.
        Useful when this server is NOT the origin and needs to know
        where to forward."""
        if not self._lock.is_distributed:
            # In single-server mode, the only origin is local.
            return self._sid if await self.is_origin_for(call_id) else None
        try:
            redis = self._lock._redis
            v = await redis.get(f"helen:lock:call:origin:{call_id}")
            if v is None:
                return None
            # Lock value is the token, not the server_id. We need a
            # separate lookup → store server_id alongside in a parallel
            # key so we can resolve.
            sid = await redis.get(f"helen:origin:{call_id}")
            if sid is None:
                return None
            return sid.decode("utf-8") if isinstance(sid, bytes) else sid
        except Exception as e:
            logger.warning("get_origin_failed", call_id=call_id, error=str(e))
            return None

    async def release(self, call_id: str) -> None:
        """Release origin lease (call ended). Idempotent."""
        h = self._holders.pop(call_id, None)
        if h is None:
            return
        await h.release()
        # Also drop the parallel server_id pointer.
        if self._lock.is_distributed:
            try:
                redis = self._lock._redis
                await redis.delete(f"helen:origin:{call_id}")
            except Exception:
                pass
        logger.info("origin_released", call_id=call_id, server_id=self._sid)

    # ── Re-election sweeper ────────────────────────────────────

    async def sweeper_loop_start(self) -> None:
        """Start a background coroutine that watches for dead servers
        and re-elects origin for their abandoned calls."""
        if self._sweeper_task is not None:
            return
        self._sweeper_task = asyncio.create_task(self._sweeper_loop())

    async def _sweeper_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=15)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._sweep_once()
                except Exception as e:
                    logger.warning("origin_sweeper_iteration_failed", error=str(e))
        except asyncio.CancelledError:
            return

    async def _sweep_once(self) -> None:
        """Find dead servers; for each abandoned call, attempt
        re-election. Only one healthy server will succeed (the lock
        is distributed)."""
        dead_ids = await self._registry.find_unhealthy(DEAD_SERVER_THRESHOLD_SEC)
        if not dead_ids:
            return
        # The actual list of calls owned by each dead server is
        # canonically in Postgres (active_calls.origin_server_id). The
        # service that wires this in (call_state_persistence) feeds us
        # via `re_elect_calls_owned_by(server_id)`. Without that wire,
        # the sweeper has nothing to do.
        for dead in dead_ids:
            await self.re_elect_calls_owned_by(dead)

    async def re_elect_calls_owned_by(self, dead_server_id: str) -> int:
        """Find calls whose origin_server_id == dead_server_id (per
        Postgres) and attempt to claim them. Returns count of
        successful claims. Idempotent — running twice does no
        additional damage."""
        # We import lazily to avoid a circular import at module load.
        try:
            from app.services.call_state_persistence import call_state_persistence
        except Exception:
            return 0

        try:
            abandoned_call_ids = await call_state_persistence.list_owned_by(dead_server_id)
        except AttributeError:
            # call_state_persistence doesn't yet expose list_owned_by.
            # That's the wiring point we'll add in the canary
            # migration; until then, no-op.
            return 0
        except Exception as e:
            logger.warning("origin_sweeper_listing_failed",
                           dead=dead_server_id, error=str(e))
            return 0

        claimed = 0
        for call_id in abandoned_call_ids:
            # Mark the dead server unhealthy in registry so other
            # servers stop forwarding to it.
            await self._registry.mark_unhealthy(
                dead_server_id, reason="origin_sweep",
            )
            ok = await self.claim_origin(call_id)
            if ok:
                claimed += 1
                # Persist the new origin in Postgres so other servers
                # see it on next refresh.
                try:
                    await call_state_persistence.update_origin(
                        call_id, self._sid,
                    )
                except Exception as e:
                    logger.warning("update_origin_persist_failed",
                                   call_id=call_id, error=str(e))
        if claimed:
            logger.info("origin_sweeper_re_elected",
                        dead=dead_server_id, claimed=claimed)
        return claimed

    # ── Stop / shutdown ────────────────────────────────────────

    async def stop(self) -> None:
        self._stopped.set()
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except (asyncio.CancelledError, BaseException):
                pass
            self._sweeper_task = None
        # Release every held lease so the next origin can claim
        # immediately, instead of waiting for TTL.
        for call_id in list(self._holders.keys()):
            await self.release(call_id)


class _LeaseHolder:
    """Wraps a ``_HeldLock`` with origin-specific bookkeeping. We
    keep this thin — the actual lock mechanics live in
    ``distributed_lock_service``."""

    def __init__(
        self,
        svc: OriginElectionService,
        call_id: str,
        held,
    ):
        self._svc = svc
        self._call_id = call_id
        self._held = held
        self._claimed_at = time.time()

    @property
    def active(self) -> bool:
        return self._held.acquired

    async def watch(self) -> None:
        """Wait until the renewal loop reports lease loss, then fire
        the on_lease_lost hook. This runs as a background task spawned
        by claim_origin."""
        # The _HeldLock renewal task sets ``token = None`` on lease
        # loss. Poll until that happens or release() is called.
        while self._held.acquired:
            await asyncio.sleep(LEASE_RENEW_EVERY_SEC / 2)
        # Lease lost while we still expect to own it → fire hook.
        if self._call_id in self._svc._holders:
            self._svc._holders.pop(self._call_id, None)
            if self._svc._on_lease_lost is not None:
                try:
                    await self._svc._on_lease_lost(self._call_id)
                except Exception as e:
                    logger.warning(
                        "on_lease_lost_handler_failed",
                        call_id=self._call_id, error=str(e),
                    )

    async def release(self) -> None:
        await self._held.release()


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[OriginElectionService] = None


def get_origin_election_service() -> OriginElectionService:
    global _svc
    if _svc is None:
        from app.services.distributed_lock_service import get_lock_service
        from app.services.server_registry_service import get_registry_service
        _svc = OriginElectionService(
            lock_service=get_lock_service(),
            registry_service=get_registry_service(),
            this_server_id="local",
        )
    return _svc


def configure(*, lock_service, registry_service, this_server_id: str) -> OriginElectionService:
    global _svc
    _svc = OriginElectionService(
        lock_service=lock_service,
        registry_service=registry_service,
        this_server_id=this_server_id,
    )
    logger.info("origin_election_service_configured", server_id=this_server_id)
    return _svc
