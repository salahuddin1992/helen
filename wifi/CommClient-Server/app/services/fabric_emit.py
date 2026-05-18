"""
fabric_emit — high-level helper that wraps a Socket.IO emit with the
new event_envelope + route_executor pipeline.

Migration story
---------------
Today every handler calls ``emit_to_user(event, payload, user_id)``
directly. That works for single-server LAN and degrades to
federation HTTP fanout for cross-server. It has none of the new
benefits (tracing, idempotency, retry, ACK, priority queueing).

We can't migrate all 100+ emit sites at once. ``fabric_emit`` is the
incremental boundary: a handler swaps its ``emit_to_user(...)`` call
for ``fabric_emit.emit_event(...)`` and immediately gets the full
fabric treatment when the env flag opts the event type in. Outside
that flag, it falls through to plain ``emit_to_user`` — zero
behavior change.

Per-event-type opt-in via env flag
----------------------------------
::

    HELEN_FABRIC_EVENT_ALLOWLIST="call.incoming,call.signal.offer"

Comma-separated. Wildcards: ``call.*`` matches any event_type
starting with ``call.``. Default empty (everything legacy).

The flag exists so we can canary one event at a time, validate via
trace dashboards, then enable more. Production rollout is then a
config change, not a code change.

API
---
    >>> ok = await fabric_emit.emit_event(
    ...     event_type="call.incoming",
    ...     priority="P1",
    ...     payload={"call_id": "...", "caller_id": "..."},
    ...     destination_user_id="user_b",
    ...     source_user_id="user_a",
    ...     call_id="call_xyz",
    ...     idempotency_key="call_initiate:abc",
    ... )
    >>> # ok=True if the envelope was accepted by the executor (queued
    >>> # for delivery). ACK arrives asynchronously via fabric.ack.*.

Falls back to plain ``emit_to_user`` for legacy event_types so
existing callers don't change behavior until they opt in.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Optional

from app.core.logging import get_logger
from app.services.event_envelope import Envelope, Priority

logger = get_logger(__name__)


def _allowlist() -> list[str]:
    raw = os.environ.get("HELEN_FABRIC_EVENT_ALLOWLIST", "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _is_fabric_enabled_for(event_type: str) -> bool:
    """Return True if ``event_type`` is in the allowlist (exact or
    wildcard). Hot — re-reads env every call so a config change
    takes effect without restart."""
    patterns = _allowlist()
    if not patterns:
        return False
    for p in patterns:
        if "*" in p:
            if fnmatch.fnmatch(event_type, p):
                return True
        elif p == event_type:
            return True
    return False


async def emit_event(
    *,
    event_type: str,
    priority: Priority,
    payload: dict[str, Any],
    destination_user_id: str,
    source_user_id: Optional[str] = None,
    call_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    requires_ack: Optional[bool] = None,
    ttl_ms: Optional[int] = None,
    sequence: Optional[int] = None,
) -> bool:
    """Send ``payload`` to ``destination_user_id`` for ``event_type``.

    Returns True on accepted-into-transport. Behavior depends on the
    fabric allowlist:

    * **In allowlist** → wrapped in ``Envelope.new()`` and routed
      through ``route_executor.execute()``. You get tracing,
      idempotency, ACK, retry, DLQ.
    * **Not in allowlist** → plain ``emit_to_user(event, payload,
      uid)``. Same shape as today.

    The legacy fallback ensures we can canary one event type at a
    time without changing every emit site simultaneously.
    """
    if _is_fabric_enabled_for(event_type):
        ok = await _emit_via_fabric(
            event_type=event_type,
            priority=priority,
            payload=payload,
            destination_user_id=destination_user_id,
            source_user_id=source_user_id,
            call_id=call_id,
            channel_id=channel_id,
            idempotency_key=idempotency_key,
            requires_ack=requires_ack,
            ttl_ms=ttl_ms,
            sequence=sequence,
        )
        if ok:
            return True
        # Fabric path failed (executor rejected, no presence, etc.).
        # Fall through to legacy emit so the user still gets the
        # event — better to deliver without trace than not at all.
        logger.warning(
            "fabric_emit_fallback_to_legacy",
            event_type=event_type,
            destination_user_id=destination_user_id,
        )

    # Legacy path — same shape as today's emit_to_user calls.
    try:
        from app.socket.server import emit_to_user
        await emit_to_user(event_type, payload, destination_user_id)
        return True
    except Exception as e:
        logger.warning(
            "legacy_emit_failed",
            event_type=event_type,
            destination_user_id=destination_user_id,
            error=str(e),
        )
        return False


async def emit_broadcast(
    *,
    event_type: str,
    priority: Priority,
    payload: dict[str, Any],
    channel_id: Optional[str] = None,
    source_user_id: Optional[str] = None,
    call_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    ttl_ms: Optional[int] = None,
    sequence: Optional[int] = None,
) -> bool:
    """Broadcast variant — fan out to every connected member of a
    channel (or globally when ``channel_id`` is None).

    Routing
    -------
    * In allowlist → publish on broker subject ``fabric.broadcast.
      {channel_id}`` (or ``fabric.broadcast.global`` for null channel).
      Subscribers on every Helen server pick up and dispatch
      locally. The fabric subscriber wires this so cross-server
      coverage is automatic.
    * Not in allowlist → fall through to legacy ``sio.emit(event,
      payload)`` (global broadcast) — same shape as today.

    Idempotency on broadcasts is best-effort; the server-side
    consumers don't dedupe currently. The caller may still pass a
    key for future tightening.

    Hard guards (mirror of envelope schema):
      * Payload size <= 8 KB.
      * Plane = "control" only.
      * P0 broadcasts are allowed but rare — typically presence flips
        are P3 (best-effort, drop oldest).
    """
    if _is_fabric_enabled_for(event_type):
        ok = await _emit_broadcast_via_fabric(
            event_type=event_type,
            priority=priority,
            payload=payload,
            channel_id=channel_id,
            source_user_id=source_user_id,
            call_id=call_id,
            idempotency_key=idempotency_key,
            ttl_ms=ttl_ms,
            sequence=sequence,
        )
        if ok:
            return True
        logger.warning(
            "fabric_broadcast_fallback_to_legacy",
            event_type=event_type, channel_id=channel_id,
        )

    # Legacy path — sio.emit globally (no room scoping by default;
    # caller is asking for a broadcast).
    try:
        from app.socket.server import sio
        await sio.emit(event_type, payload)
        return True
    except Exception as e:
        logger.warning(
            "legacy_broadcast_failed",
            event_type=event_type, error=str(e),
        )
        return False


async def _emit_broadcast_via_fabric(
    *,
    event_type: str,
    priority: Priority,
    payload: dict[str, Any],
    channel_id: Optional[str],
    source_user_id: Optional[str],
    call_id: Optional[str],
    idempotency_key: Optional[str],
    ttl_ms: Optional[int],
    sequence: Optional[int],
) -> bool:
    try:
        from app.services.discovery_service import get_server_id
        from app.services.broker_client import get_broker
    except Exception as e:
        logger.warning("fabric_imports_failed", error=str(e))
        return False
    broker = get_broker()
    if broker is None:
        logger.warning("fabric_broker_unconfigured", event_type=event_type)
        return False

    this_server_id = get_server_id()
    try:
        env = Envelope.new(
            event_type=event_type,
            priority=priority,
            source_server_id=this_server_id,
            source_user_id=source_user_id,
            destination_user_id=None,  # broadcast
            destination_server_id=None,
            call_id=call_id,
            channel_id=channel_id,
            idempotency_key=idempotency_key,
            ttl_ms=ttl_ms,
            sequence=sequence,
            requires_ack=False,  # broadcasts don't ack
            payload=payload,
        )
    except Exception as e:
        logger.warning(
            "fabric_broadcast_envelope_failed",
            event_type=event_type, error=str(e),
        )
        return False

    subject = f"fabric.broadcast.{channel_id or 'global'}"
    return await broker.publish(subject, env)


async def _emit_via_fabric(
    *,
    event_type: str,
    priority: Priority,
    payload: dict[str, Any],
    destination_user_id: str,
    source_user_id: Optional[str],
    call_id: Optional[str],
    channel_id: Optional[str],
    idempotency_key: Optional[str],
    requires_ack: Optional[bool],
    ttl_ms: Optional[int],
    sequence: Optional[int],
) -> bool:
    try:
        from app.services.discovery_service import get_server_id
        from app.services.route_executor import get_executor
        from app.services.trace_collector_service import trace_collector
    except Exception as e:
        logger.warning("fabric_imports_failed", error=str(e))
        return False

    executor = get_executor()
    if executor is None:
        logger.warning("fabric_executor_unconfigured", event_type=event_type)
        return False

    this_server_id = get_server_id()

    try:
        env = Envelope.new(
            event_type=event_type,
            priority=priority,
            source_server_id=this_server_id,
            source_user_id=source_user_id,
            destination_user_id=destination_user_id,
            call_id=call_id,
            channel_id=channel_id,
            idempotency_key=idempotency_key,
            requires_ack=requires_ack,
            ttl_ms=ttl_ms,
            sequence=sequence,
            payload=payload,
        )
    except Exception as e:
        # Most likely PayloadTooLarge — the caller is shipping a
        # binary or a very large blob. Refuse to send through control
        # plane; legacy emit will also fail but with a clearer message.
        logger.warning(
            "fabric_envelope_construction_failed",
            event_type=event_type, error=str(e),
        )
        return False

    # Record the producer-side hop ("forwarded" from the producer's
    # perspective — the local executor will record subsequent hops).
    try:
        await trace_collector.record_hop(
            env, action="forwarded",
            next_server_id=None,  # not yet planned
        )
    except Exception:
        pass

    try:
        return await executor.execute(env)
    except Exception as e:
        logger.warning(
            "fabric_executor_threw",
            event_id=env.event_id,
            event_type=event_type,
            error=str(e),
        )
        return False
