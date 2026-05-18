"""
Server fabric subscribers — the receive-side of the broker.

When ``route_executor`` publishes an envelope to a subject scoped to
some destination server, the destination server needs subscribers
that pull from that subject and dispatch the envelope into local
delivery (Socket.IO emit_to_user) or further forward it.

Subjects this module subscribes to
----------------------------------
  fabric.{P0..P4}.>.{this_server_id}    # any envelope targeting us
  fabric.broadcast.>                    # channel-wide broadcasts
  fabric.ack.{event_id}                 # ACK return path

Per priority we run a dedicated consumer task so a P3 backlog can't
starve P0. Each task is wrapped in a watchdog that restarts it if
it crashes, so a malformed envelope can never permanently take down
a priority's consumer.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger
from app.services.event_envelope import Envelope

logger = get_logger(__name__)

# One task per priority. Pattern wildcards are translated to fnmatch
# in BrokerClient's in-process path; for Redis we resolve to stream
# keys at subscribe time.
PRIORITY_PATTERNS = {
    "P0": "fabric.P0.*",
    "P1": "fabric.P1.*",
    "P2": "fabric.P2.*",
    "P3": "fabric.P3.*",
    "P4": "fabric.P4.*",
}

# Subscription pattern for ACKs that target us. ACKs flow back along
# the reverse path; producer's ack_manager is keyed by event_id.
ACK_PATTERN = "fabric.ack.*"

# Broadcasts fan out to every server. We subscribe to the global
# pattern; receivers locally re-emit via sio.emit so all clients on
# this server get the event.
BROADCAST_PATTERN = "fabric.broadcast.*"


class FabricSubscribers:
    """Manages the lifecycle of background consumer tasks for the
    broker fabric."""

    def __init__(
        self,
        *,
        this_server_id: str,
        broker_client,
        ack_manager,
        local_deliver_fn,
        priority_router=None,
    ) -> None:
        self._sid = this_server_id
        self._broker = broker_client
        self._ack = ack_manager
        self._local_deliver = local_deliver_fn
        self._priority_router = priority_router
        self._tasks: list[asyncio.Task] = []
        self._stopped = asyncio.Event()
        self._metrics = {
            "received_total": 0,
            "delivered_local": 0,
            "ack_received": 0,
            "deliver_failed": 0,
            "consumer_restarts": 0,
        }

    async def start(self) -> None:
        if self._tasks:
            return
        # One task per priority queue.
        for priority in PRIORITY_PATTERNS:
            t = asyncio.create_task(
                self._consume_with_restart(priority),
                name=f"fabric_consumer_{priority}",
            )
            self._tasks.append(t)
        # ACK consumer.
        t = asyncio.create_task(
            self._consume_acks_with_restart(),
            name="fabric_ack_consumer",
        )
        self._tasks.append(t)
        # Broadcast consumer — re-emits via sio.emit so all locally-
        # connected clients see the event without our server having
        # to be the original producer.
        t = asyncio.create_task(
            self._consume_broadcasts_with_restart(),
            name="fabric_broadcast_consumer",
        )
        self._tasks.append(t)
        logger.info("fabric_subscribers_started", server_id=self._sid,
                    consumers=len(self._tasks))

    async def stop(self) -> None:
        self._stopped.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, BaseException):
                pass
        self._tasks.clear()

    def metrics(self) -> dict:
        return dict(self._metrics)

    # ── Consumer with restart watchdog ────────────────────────

    async def _consume_with_restart(self, priority: str) -> None:
        """Run the consumer loop; if it crashes, log + restart with
        modest backoff. A single poison pill should NOT permanently
        kill the consumer — it's already ACK'd inside broker_client."""
        attempt = 0
        while not self._stopped.is_set():
            try:
                await self._consume_priority(priority)
                if self._stopped.is_set():
                    return
                # Loop returned normally (e.g. no streams yet) —
                # back off briefly before re-subscribing.
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return
            except Exception as e:
                attempt += 1
                self._metrics["consumer_restarts"] += 1
                backoff = min(30.0, 1.0 * (2 ** min(attempt, 5)))
                logger.warning(
                    "fabric_consumer_crashed",
                    priority=priority, error=str(e),
                    backoff_sec=backoff,
                )
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _consume_priority(self, priority: str) -> None:
        pattern = PRIORITY_PATTERNS[priority]
        async for env in self._broker.subscribe(pattern):
            self._metrics["received_total"] += 1
            try:
                await self._dispatch(env)
            except Exception as e:
                logger.warning(
                    "fabric_dispatch_threw",
                    event_id=env.event_id, error=str(e),
                )

    async def _consume_broadcasts_with_restart(self) -> None:
        """Pull broadcast envelopes off the broker and re-emit them
        locally via sio.emit. Critical for cross-server presence /
        typing / similar fan-out events."""
        attempt = 0
        while not self._stopped.is_set():
            try:
                async for env in self._broker.subscribe(BROADCAST_PATTERN):
                    self._metrics["received_total"] += 1
                    try:
                        from app.socket.server import sio as _sio
                        # If channel_id is set, target the room; else
                        # global broadcast.
                        if env.channel_id:
                            from app.socket import channel_room as _channel_room
                            try:
                                await _channel_room.ensure_populated(
                                    _sio, env.channel_id,
                                )
                                room = _channel_room.room_name(env.channel_id)
                                await _sio.emit(env.event_type, env.payload, room=room)
                            except Exception:
                                # Fall back to global if room machinery isn't ready.
                                await _sio.emit(env.event_type, env.payload)
                        else:
                            await _sio.emit(env.event_type, env.payload)
                        # Trace the delivery.
                        try:
                            from app.services.trace_collector_service import trace_collector
                            await trace_collector.record_hop(env, action="delivered")
                        except Exception:
                            pass
                        self._metrics["delivered_local"] += 1
                    except Exception as e:
                        logger.warning(
                            "fabric_broadcast_dispatch_failed",
                            event_id=env.event_id, error=str(e),
                        )
                        self._metrics["deliver_failed"] += 1
                if self._stopped.is_set():
                    return
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return
            except Exception as e:
                attempt += 1
                self._metrics["consumer_restarts"] += 1
                backoff = min(30.0, 1.0 * (2 ** min(attempt, 5)))
                logger.warning(
                    "broadcast_consumer_crashed", error=str(e), backoff_sec=backoff,
                )
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _consume_acks_with_restart(self) -> None:
        attempt = 0
        while not self._stopped.is_set():
            try:
                async for env in self._broker.subscribe(ACK_PATTERN):
                    self._metrics["ack_received"] += 1
                    # ACK envelope payload carries the original
                    # event_id under "for_event_id". The producer's
                    # ack_manager records it.
                    eid = env.payload.get("for_event_id")
                    if eid and self._ack is not None:
                        await self._ack.record_ack(eid)
                if self._stopped.is_set():
                    return
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return
            except Exception as e:
                attempt += 1
                self._metrics["consumer_restarts"] += 1
                backoff = min(30.0, 1.0 * (2 ** min(attempt, 5)))
                logger.warning(
                    "ack_consumer_crashed", error=str(e), backoff_sec=backoff,
                )
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass

    # ── Dispatch ──────────────────────────────────────────────

    async def _dispatch(self, env: Envelope) -> None:
        """An envelope arrived for us via the broker. Three cases:

        1. We are the destination_server_id → deliver locally.
        2. destination_server_id is unset but destination_user_id
           resolves to us → deliver locally.
        3. destination_server_id is some other server →
           re-publish to the next hop. (This happens in chaos
           chain mode where every server is a hop.)

        We delegate cases 1+2 to ``local_deliver_fn``. Case 3 calls
        back into ``route_executor.execute()`` with the same envelope
        — the executor's loop detection prevents infinite forwards
        if a route oscillates.
        """
        # Peer authorization gate — refuse events arriving from a
        # peer not currently in READY/DEGRADED state. Local events
        # (source_server_id == self) skip the gate. Without this,
        # any peer that managed to publish to a broker stream we
        # subscribe to (e.g. shared Redis with stale credentials)
        # could inject events even after admin denial.
        if env.source_server_id and env.source_server_id != self._sid:
            try:
                from app.services.peer_approval_service import peer_approval_service
                allowed = await peer_approval_service.is_peer_routable(
                    env.source_server_id,
                )
            except Exception as e:
                logger.warning("fabric_peer_gate_failed",
                               server_id=env.source_server_id, error=str(e))
                allowed = False
            if not allowed:
                self._metrics.setdefault("blocked_unapproved_source", 0)
                self._metrics["blocked_unapproved_source"] += 1
                logger.warning(
                    "fabric_blocked_unapproved_peer",
                    source=env.source_server_id,
                    event_id=env.event_id,
                    event_type=env.event_type,
                )
                # Don't ACK or forward; just drop.
                return

        # Send ACK back to the previous hop / producer immediately
        # if requires_ack — receivers ACK on enqueue, not on
        # delivery. This matches the principle: broker accept = ACK.
        if env.requires_ack:
            await self._send_ack(env)

        if env.destination_server_id == self._sid:
            ok = await self._local_deliver(env) if self._local_deliver else False
            self._metrics["delivered_local"] += int(bool(ok))
            if not ok:
                self._metrics["deliver_failed"] += 1
            return

        # destination is elsewhere → forward via executor.
        try:
            from app.services.route_executor import get_executor
            ex = get_executor()
        except Exception:
            ex = None
        if ex is None:
            self._metrics["deliver_failed"] += 1
            logger.warning(
                "fabric_no_executor_for_forward",
                event_id=env.event_id,
                destination_server_id=env.destination_server_id,
            )
            return
        await ex.execute(env)

    async def _send_ack(self, env: Envelope) -> None:
        """Publish an ACK envelope back along the reverse path.
        ACKs are P0 fire-and-forget — no further ACK on the ACK."""
        ack_env = Envelope.new(
            event_type="fabric.ack",
            priority="P0",
            source_server_id=self._sid,
            destination_server_id=env.source_server_id,
            payload={"for_event_id": env.event_id},
            requires_ack=False,
            ttl_ms=2000,
        )
        # The ACK's `requires_ack` was forced to False above in
        # event_envelope.new() — but P0 enforces requires_ack=True
        # at the schema level. Override after construction.
        ack_env.requires_ack = False
        try:
            await self._broker.publish(f"fabric.ack.{env.event_id}", ack_env)
        except Exception as e:
            logger.warning(
                "ack_publish_failed",
                for_event_id=env.event_id, error=str(e),
            )


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[FabricSubscribers] = None


def get_fabric_subscribers() -> Optional[FabricSubscribers]:
    return _svc


def configure(**kwargs) -> FabricSubscribers:
    global _svc
    _svc = FabricSubscribers(**kwargs)
    return _svc
