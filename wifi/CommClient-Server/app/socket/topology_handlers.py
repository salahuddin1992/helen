"""
Socket.IO handlers for hybrid mesh/SFU topology management.

Events handled
--------------
* ``call_topology_ack``        — client confirms it switched to a new generation
* ``call_topology_request``    — client asks server to force a topology
* ``call_signal_replay``       — client reconnects mid-call and wants the
                                 last N signal events to rebuild its peer state
* ``call_quality_report``      — client publishes its packet-loss/RTT sample
                                 (same data is stored in the active_call_participants
                                 row and fed to QualityOracle)
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger
from app.services.call_service import call_service
from app.services.call_state_persistence import call_state_persistence
from app.services.topology_manager import QualitySample, topology_manager
from app.socket.server import sio

logger = get_logger(__name__)


async def _auth_user(sid: str) -> str | None:
    """Extract authenticated user id from the socket session. Returns None if anonymous."""
    try:
        session = await sio.get_session(sid)
    except Exception:
        return None
    if not session:
        return None
    return session.get("user_id")


@sio.on("call_topology_ack")
async def _on_topology_ack(sid: str, data: dict[str, Any]) -> dict[str, Any]:
    user_id = await _auth_user(sid)
    if not user_id:
        return {"ok": False, "error": "unauthenticated"}

    call_id = (data or {}).get("call_id")
    generation = int((data or {}).get("generation", 0))
    if not call_id:
        return {"ok": False, "error": "call_id required"}

    expected = topology_manager.current_generation(call_id)
    ok = generation == expected
    logger.info(
        "topology_ack",
        call_id=call_id, user=user_id, ack=generation, expected=expected, ok=ok,
    )
    return {"ok": ok, "expected_generation": expected}


@sio.on("call_topology_request")
async def _on_topology_request(sid: str, data: dict[str, Any]) -> dict[str, Any]:
    user_id = await _auth_user(sid)
    if not user_id:
        return {"ok": False, "error": "unauthenticated"}

    call_id = (data or {}).get("call_id")
    requested = (data or {}).get("routing")
    if not call_id or requested not in {"p2p", "mesh", "sfu", "hybrid"}:
        return {"ok": False, "error": "invalid params"}

    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return {"ok": False, "error": "not in call"}

    new_routing = await topology_manager.force_switch(
        call, requested, reason="manual",
    )
    return {"ok": True, "routing": new_routing, "generation": topology_manager.current_generation(call_id)}


@sio.on("call_signal_replay")
async def _on_signal_replay(sid: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    A reconnecting client (or a worker that just rehydrated) asks for the
    recent signaling history so it can rebuild peer connections without
    restarting the call.
    """
    user_id = await _auth_user(sid)
    if not user_id:
        return {"ok": False, "error": "unauthenticated"}

    call_id = (data or {}).get("call_id")
    since = (data or {}).get("since_generation")
    if not call_id:
        return {"ok": False, "error": "call_id required"}

    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return {"ok": False, "error": "not in call"}

    signals, truncated = await call_state_persistence.replay_signals(
        call_id, for_user=user_id,
        since_generation=int(since) if since is not None else None,
    )
    return {
        "ok": True,
        "signals": signals,
        "truncated": truncated,
        "generation": topology_manager.current_generation(call_id),
        "routing": call.routing,
    }


@sio.on("call_heartbeat")
async def _on_call_heartbeat(sid: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Periodic keepalive from each call participant. Without this, the
    ``CallStatePersistence.sweep_orphans`` reaper marks calls as ended
    after ``HEARTBEAT_STALE_SECONDS`` (90s) of inactivity.

    Clients are expected to emit this every ~20s while in an active call
    (see ``CallEngine._startCallHeartbeat`` on the desktop).
    """
    user_id = await _auth_user(sid)
    if not user_id:
        return {"ok": False, "error": "unauthenticated"}

    call_id = (data or {}).get("call_id")
    if not call_id:
        return {"ok": False, "error": "call_id required"}

    # Cheap server-side validation. In a federated cluster the call may
    # live on a sibling server — reject only if the DB doesn't know the
    # call AND the in-memory state doesn't either.
    #
    # BLOCKER-1 fix: before this change, any call_heartbeat that landed
    # on a non-origin server returned {"error": "not in call"} because
    # in-memory state was empty. The orphan sweep then killed the call
    # at 90s. Now we still touch the DB heartbeat row from any server,
    # AND when the call is remote we forward the ack via federation
    # RPC so the origin's in-memory state stays warm too.
    call = call_service.get_call(call_id)
    if not call:
        # Not in local memory — but the DB might know about it.
        from app.services.call_signal_authz import call_signal_authz as _csa
        from app.services.discovery_service import get_server_id as _my_id
        origin = _csa.origin_of(call_id)
        if origin and origin != _my_id():
            # Forward heartbeat to origin so its orphan sweep sees it.
            try:
                from app.services.federation_service import federation_service
                await federation_service.forward_call_rpc(
                    origin, "heartbeat", call_id, user_id,
                )
            except Exception as _e:
                logger.debug("call_heartbeat_forward_failed",
                             call_id=call_id, error=str(_e))
            # Also bump local DB row (the schema is shared, so the
            # last_heartbeat_at field protects against the orphan
            # sweep regardless of which server saw the heartbeat).
            try:
                await call_state_persistence.heartbeat(call_id)
            except Exception:
                pass
            return {"ok": True, "server_ts": int(__import__("time").time() * 1000),
                    "forwarded_to": origin[:12]}
        return {"ok": False, "error": "not in call"}

    if user_id not in call.participants:
        return {"ok": False, "error": "not in call"}

    # Stamp last-active so the per-participant idle eviction loop
    # leaves this peer alone. Without this, only the call-row
    # heartbeat advances; idle eviction would scrape away anyone
    # whose "joined_at" was more than 45s ago.
    try:
        from datetime import datetime as _dt, timezone as _tz
        call.participants[user_id]["last_active_at"] = _dt.now(_tz.utc)
    except Exception:
        pass

    try:
        await call_state_persistence.heartbeat(call_id)
    except Exception as exc:
        logger.debug("call_heartbeat_persist_failed", call_id=call_id, error=str(exc))

    return {"ok": True, "server_ts": int(__import__("time").time() * 1000)}


@sio.on("call_quality_report")
async def _on_quality_report(sid: str, data: dict[str, Any]) -> dict[str, Any]:
    user_id = await _auth_user(sid)
    if not user_id:
        return {"ok": False, "error": "unauthenticated"}

    call_id = (data or {}).get("call_id")
    if not call_id:
        return {"ok": False, "error": "call_id required"}

    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return {"ok": False, "error": "not in call"}

    try:
        packet_loss = float((data or {}).get("packet_loss", 0.0) or 0.0)
        rtt_ms = float((data or {}).get("rtt_ms", 0.0) or 0.0)
        jitter_ms = float((data or {}).get("jitter_ms", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid metrics"}

    topology_manager.quality.record(
        call_id, user_id,
        QualitySample(packet_loss=packet_loss, rtt_ms=rtt_ms, jitter_ms=jitter_ms),
    )
    try:
        await call_state_persistence.record_quality(
            call_id, user_id,
            {"packet_loss": packet_loss, "rtt_ms": rtt_ms, "jitter_ms": jitter_ms},
        )
    except Exception:
        pass

    # Consider upgrading to SFU under sustained bad quality.
    try:
        await topology_manager.reevaluate(call)
    except Exception as exc:
        logger.debug("reevaluate_after_quality_failed", error=str(exc))

    return {"ok": True}
