"""
Route executor — the orchestrator that ties together every Tier S/A
service to actually deliver an envelope.

Flow
----
::

    execute(envelope) →
        validate (size, expiry, plane)        ← Envelope schema guards
        check loop                            ← envelope.is_loop(this)
        resolve destination                   ← presence_service
        local delivery? → deliver_local()     ← short-circuit
        plan route                            ← route_planner
        publish to broker (next hop)          ← broker_client
        track ACK (if requires_ack)           ← event_ack_manager
        record DLQ on terminal failure        ← dead_letter_service

Local delivery is the optimistic short-circuit: if the destination
user is hosted on this server, we skip routing and emit directly via
Socket.IO. This is the fast path that production deployments hit
99% of the time — the broker is only used when the destination is on
another server.

API
---
    >>> executor = RouteExecutor(...)
    >>> ok = await executor.execute(envelope)
    >>> # True = accepted into transport (broker / local emit)
    >>> # False = rejected (loop, expired, dest unknown, etc.)

Wiring
------
``configure(...)`` plugs in the dependencies. The executor itself
holds no state — it's stateless orchestration over the Tier S/A
services.
"""

from __future__ import annotations

import os
from typing import Optional, Callable, Awaitable

from app.core.logging import get_logger
from app.services.event_envelope import Envelope, MaxHopsExceeded

logger = get_logger(__name__)

DeliverFn = Callable[[Envelope], Awaitable[bool]]
"""Local delivery hook — invoked when the executor determines the
destination user is on this server. Returns True on accepted-by-
local-Socket.IO. The caller (``configure(...)``) provides this so the
executor stays decoupled from socket internals."""


class RouteExecutor:
    def __init__(
        self,
        *,
        this_server_id: str,
        presence_service,
        registry_service,
        route_planner,
        broker_client,
        ack_manager,
        local_deliver_fn: Optional[DeliverFn] = None,
        dlq_recorder: Optional[Callable] = None,
    ) -> None:
        self._sid = this_server_id
        self._presence = presence_service
        self._registry = registry_service
        self._planner = route_planner
        self._broker = broker_client
        self._ack = ack_manager
        self._local_deliver = local_deliver_fn
        self._dlq = dlq_recorder
        self._metrics = {
            "executed": 0,
            "delivered_local": 0,
            "forwarded": 0,
            "loop_blocked": 0,
            "expired": 0,
            "max_hops": 0,
            "destination_unknown": 0,
            "publish_failed": 0,
            "blocked_unapproved_source": 0,
        }

    @property
    def server_id(self) -> str:
        return self._sid

    def metrics(self) -> dict:
        return dict(self._metrics)

    # ── Main entry ─────────────────────────────────────────────

    async def execute(self, env: Envelope) -> bool:
        self._metrics["executed"] += 1

        # 1. Expiry — drop stale events early.
        if env.is_expired():
            self._metrics["expired"] += 1
            await self._record_dlq(env, "expired_at_executor")
            return False

        # 2. Loop detection — if we're seeing this event again it's
        # a routing bug or a hostile chain. Drop and DLQ.
        if env.is_loop(self._sid):
            self._metrics["loop_blocked"] += 1
            logger.warning(
                "route_loop_blocked",
                event_id=env.event_id,
                source=env.source_server_id,
                hop_index=env.hop_index,
            )
            await self._record_dlq(env, "loop_detected")
            return False

        # 3. Peer authorization gate — only events FROM a READY peer
        # may continue through the fabric. We skip the gate for events
        # the local server is producing itself (source_server_id ==
        # self), since those are already trusted by virtue of being
        # generated locally. Forwarded events from sibling servers go
        # through this gate before consuming any further capacity.
        if env.source_server_id and env.source_server_id != self._sid:
            try:
                from app.services.peer_approval_service import peer_approval_service
                allowed = await peer_approval_service.is_peer_routable(
                    env.source_server_id,
                )
            except Exception as e:
                logger.warning(
                    "peer_gate_check_failed",
                    server_id=env.source_server_id, error=str(e),
                )
                allowed = False
            if not allowed:
                self._metrics.setdefault("blocked_unapproved_source", 0)
                self._metrics["blocked_unapproved_source"] += 1
                logger.warning(
                    "route_blocked_unapproved_peer",
                    source=env.source_server_id,
                    event_id=env.event_id,
                    event_type=env.event_type,
                )
                await self._record_dlq(env, "source_peer_not_approved")
                return False

        # 3. Resolve destination if we have a destination_user_id.
        # If we already have destination_server_id from caller, trust it.
        if env.destination_user_id and not env.destination_server_id:
            try:
                dest_server = await self._presence.get_server_for(env.destination_user_id)
            except Exception as e:
                logger.warning(
                    "presence_lookup_threw",
                    event_id=env.event_id, error=str(e),
                )
                dest_server = None
            if dest_server is None:
                self._metrics["destination_unknown"] += 1
                await self._record_dlq(env, "destination_user_offline")
                return False
            env.destination_server_id = dest_server

        # 4. Local delivery short-circuit.
        if env.destination_server_id == self._sid:
            ok = await self._deliver_local(env)
            self._metrics["delivered_local"] += int(bool(ok))
            return ok

        # 5. Plan route. If the caller already supplied a route_id we
        # could reuse a cached plan; for simplicity we re-plan every
        # call. RoutePlanner is cheap (Dijkstra over ≤ N servers).
        mode = "chaos_chain" if self._is_chaos_enabled() else "production"
        try:
            route = await self._planner.plan(
                source=env.source_server_id,
                dest=env.destination_server_id,
                mode=mode,
                trace_id=env.trace_id,
            )
        except Exception as e:
            logger.warning(
                "route_plan_failed",
                event_id=env.event_id, error=str(e),
            )
            await self._record_dlq(env, "route_plan_failed")
            return False

        # 6. Determine next hop. Walk the route to find our position.
        try:
            idx = route.index(self._sid)
        except ValueError:
            # We're not in the planned route — that's a planner bug
            # or a chaos-mode degenerate case.
            await self._record_dlq(env, "self_not_in_route")
            return False

        if idx + 1 >= len(route):
            # We're the final hop but destination_server_id != self?
            # Inconsistent state — DLQ.
            await self._record_dlq(env, "executor_terminal_mismatch")
            return False

        next_hop = route[idx + 1]
        env.next_server_id = next_hop
        if env.route_id is None:
            env.route_id = f"route_{env.trace_id}"

        # 7. Step the envelope (rotate spans, increment hop).
        try:
            next_env = env.step(next_hop)
        except MaxHopsExceeded:
            self._metrics["max_hops"] += 1
            await self._record_dlq(env, "max_hops_exceeded")
            return False

        # 8. Publish to broker.
        subject = self._subject_for(next_env, next_hop)
        if env.requires_ack and self._ack is not None:
            ok = await self._ack.track(
                next_env,
                send_fn=lambda e: self._broker.publish(subject, e),
            )
        else:
            ok = await self._broker.publish(subject, next_env)

        if not ok:
            self._metrics["publish_failed"] += 1
            await self._record_dlq(next_env, "publish_failed")
            return False

        self._metrics["forwarded"] += 1
        return True

    # ── Helpers ────────────────────────────────────────────────

    async def _deliver_local(self, env: Envelope) -> bool:
        if self._local_deliver is None:
            logger.warning(
                "no_local_deliver_fn_configured",
                event_id=env.event_id,
            )
            await self._record_dlq(env, "no_local_deliver_fn")
            return False
        try:
            return await self._local_deliver(env)
        except Exception as e:
            logger.warning(
                "local_deliver_threw",
                event_id=env.event_id, error=str(e),
            )
            await self._record_dlq(env, "local_deliver_threw")
            return False

    @staticmethod
    def _subject_for(env: Envelope, target_server_id: str) -> str:
        """Compose the broker subject for a server-targeted forward."""
        return f"fabric.{env.priority}.{env.event_type}.{target_server_id}"

    async def _record_dlq(self, env: Envelope, reason: str) -> None:
        if self._dlq is None:
            return
        try:
            await self._dlq(env, reason)
        except Exception as e:
            logger.warning("route_dlq_record_failed", reason=reason, error=str(e))

    @staticmethod
    def _is_chaos_enabled() -> bool:
        raw = os.environ.get("HELEN_ENABLE_100_HOP_TEST_MODE", "").strip().lower()
        return raw in {"1", "true", "yes", "on"}


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[RouteExecutor] = None


def get_executor() -> Optional[RouteExecutor]:
    return _svc


def configure(
    *,
    this_server_id: str,
    presence_service,
    registry_service,
    route_planner,
    broker_client,
    ack_manager,
    local_deliver_fn: Optional[DeliverFn] = None,
    dlq_recorder: Optional[Callable] = None,
) -> RouteExecutor:
    global _svc
    _svc = RouteExecutor(
        this_server_id=this_server_id,
        presence_service=presence_service,
        registry_service=registry_service,
        route_planner=route_planner,
        broker_client=broker_client,
        ack_manager=ack_manager,
        local_deliver_fn=local_deliver_fn,
        dlq_recorder=dlq_recorder,
    )
    logger.info(
        "route_executor_configured",
        server_id=this_server_id,
        chaos_enabled=RouteExecutor._is_chaos_enabled(),
    )
    return _svc
