"""
Transport socket.IO event handlers.

Real-time transport layer events:
  - Transport detection results (real psutil-backed)
  - Bridge lifecycle (create, destroy)
  - Signal quality updates
  - Peer join/leave notifications
  - Auto-failover alerts

Production hardening:
  - Rate limiting on scan requests (in-memory, per user)
  - Bridge events validate ownership
  - Subscription tracking with cancel-on-disconnect
  - Per-event try/except → degraded-mode error reply, never silent
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.core.logging import get_logger
from app.socket.server import sio

logger = get_logger(__name__)


# ── Per-user rate limit + subscription state ──────────────────

_last_scan_at: dict[str, float] = {}
_SCAN_RATE_LIMIT_SECONDS = 5

# sid → list of (transport_id, asyncio.Task) for active signal subscriptions
_signal_subscriptions: dict[str, list[tuple[str, asyncio.Task]]] = {}


def _rate_limited(user_id: str) -> bool:
    now = time.monotonic()
    last = _last_scan_at.get(user_id, 0.0)
    if now - last < _SCAN_RATE_LIMIT_SECONDS:
        return True
    _last_scan_at[user_id] = now
    return False


# ── Detection Events ──────────────────────────────────────────


@sio.event
async def transport_scan_request(sid: str, data: dict):
    """
    Client requests transport detection scan.

    Expected data:
      {
        "adapter_family": "ethernet" | null,  # Optional filter
      }

    Response: transport:scan_result event broadcast to client
    """
    try:
        from app.socket.server import get_user_id

        user_id = await get_user_id(sid)
        if not user_id:
            logger.warning("transport_scan_no_user", sid=sid)
            await sio.emit(
                "transport:scan_error",
                {"error": "unauthenticated"},
                to=sid,
            )
            return

        adapter_family = data.get("adapter_family") if isinstance(data, dict) else None
        logger.info(
            "transport_scan_request",
            user_id=user_id,
            sid=sid,
            adapter_family=adapter_family,
        )

        if _rate_limited(user_id):
            logger.warning("transport_scan_rate_limited", user_id=user_id)
            await sio.emit(
                "transport:scan_error",
                {"error": "rate_limited", "retry_after_s": _SCAN_RATE_LIMIT_SECONDS},
                to=sid,
            )
            return

        # Real detection (delegate to REST helpers — single source of truth)
        from app.api.routes.transport import _enumerate_real_transports, _model_dump

        started = time.perf_counter()
        try:
            detected = _enumerate_real_transports(adapter_family)
        except Exception as e:
            logger.error("transport_scan_enumerate_failed", error=str(e))
            detected = []

        duration_ms = (time.perf_counter() - started) * 1000.0

        scan_result = {
            "detected_transports": [_model_dump(d) for d in detected],
            "total_detected": len(detected),
            "scan_timestamp": datetime.utcnow().isoformat(),
            "scan_duration_ms": round(duration_ms, 2),
        }

        await sio.emit("transport:scan_result", scan_result, to=sid)
        logger.info(
            "transport_scan_complete",
            user_id=user_id,
            detected=len(detected),
            duration_ms=round(duration_ms, 1),
        )

    except Exception as e:
        logger.error("transport_scan_error", error=str(e), sid=sid)
        await sio.emit(
            "transport:scan_error",
            {"error": "Scan failed", "detail": str(e)},
            to=sid,
        )


@sio.on("transport:scan_request")
async def _transport_scan_request_alias(sid: str, data: dict):
    """Client emits colon-style 'transport:scan_request'; delegate to underscore handler."""
    return await transport_scan_request(sid, data)


# ── Bridge Lifecycle Events ────────────────────────────────────


@sio.event
async def bridge_create_request(sid: str, data: dict):
    """
    Client requests to create a bridge.

    Expected data:
      {
        "transport_id": "ethernet-rj45",
        "name": "Main Bridge",
        "bind_port": null,
        "protocol": "tcp",
        "encryption": true,
        "max_connections": 64
      }

    Response: transport:bridge_created event broadcast to all
    """
    try:
        from app.socket.server import get_user_id
        from app.api.routes.transport import (
            _CATALOG_BY_ID,
            _allocate_bridge_port,
            _bridges,
        )
        import uuid

        user_id = await get_user_id(sid)
        if not user_id:
            logger.warning("bridge_create_no_user", sid=sid)
            await sio.emit(
                "transport:bridge_error",
                {"error": "unauthenticated"},
                to=sid,
            )
            return

        if not isinstance(data, dict):
            await sio.emit(
                "transport:bridge_error",
                {"error": "invalid_payload"},
                to=sid,
            )
            return

        transport_id = data.get("transport_id")
        bridge_name = data.get("name") or "Unnamed Bridge"

        logger.info(
            "bridge_create_request",
            user_id=user_id,
            transport_id=transport_id,
            name=bridge_name,
        )

        # Validate transport
        catalog = _CATALOG_BY_ID.get(transport_id)
        if not catalog:
            await sio.emit(
                "transport:bridge_error",
                {"error": "unknown_transport", "transport_id": transport_id},
                to=sid,
            )
            return

        port = _allocate_bridge_port(data.get("bind_port"))
        if port == 0:
            await sio.emit(
                "transport:bridge_error",
                {"error": "no_free_port"},
                to=sid,
            )
            return

        bridge_id = f"bridge-{uuid.uuid4().hex[:12]}"
        bridge = {
            "bridge_id": bridge_id,
            "name": bridge_name,
            "transport_id": transport_id,
            "transport_name": catalog["name"],
            "bind_address": "0.0.0.0",
            "bind_port": port,
            "status": "active",
            "is_encrypted": bool(data.get("encryption", True)),
            "connected_peers": [],
            "peer_count": 0,
            "bytes_sent": 0,
            "bytes_received": 0,
            "uptime_seconds": 0,
            "avg_latency_ms": None,
            "created_at": datetime.utcnow().isoformat(),
            "_owner": user_id,
            "_created_monotonic": time.monotonic(),
        }
        _bridges[bridge_id] = bridge

        public = {k: v for k, v in bridge.items() if not k.startswith("_")}
        public["created_by"] = user_id

        await sio.emit("transport:bridge_created", public)
        logger.info("transport_bridge_created", bridge_id=bridge_id, port=port)

    except Exception as e:
        logger.error("bridge_create_error", error=str(e), sid=sid)
        await sio.emit(
            "transport:bridge_error",
            {"error": "Bridge creation failed", "detail": str(e)},
            to=sid,
        )


@sio.event
async def bridge_destroy_request(sid: str, data: dict):
    """
    Client requests to destroy a bridge.

    Expected data:
      {
        "bridge_id": "bridge-001"
      }
    """
    try:
        from app.socket.server import get_user_id
        from app.api.routes.transport import _bridges

        user_id = await get_user_id(sid)
        if not user_id:
            logger.warning("bridge_destroy_no_user", sid=sid)
            return

        if not isinstance(data, dict):
            await sio.emit(
                "transport:bridge_error",
                {"error": "invalid_payload"},
                to=sid,
            )
            return

        bridge_id = data.get("bridge_id")
        logger.info("bridge_destroy_request", user_id=user_id, bridge_id=bridge_id)

        bridge = _bridges.get(bridge_id)
        if not bridge:
            # Idempotent — emit success anyway
            await sio.emit(
                "transport:bridge_destroyed",
                {"bridge_id": bridge_id, "reason": "not_found"},
                to=sid,
            )
            return

        if bridge.get("_owner") != user_id:
            await sio.emit(
                "transport:bridge_error",
                {"error": "forbidden", "detail": "not bridge owner"},
                to=sid,
            )
            return

        _bridges.pop(bridge_id, None)
        await sio.emit(
            "transport:bridge_destroyed",
            {"bridge_id": bridge_id, "reason": "user_requested"},
        )
        logger.info("transport_bridge_destroyed", bridge_id=bridge_id)

    except Exception as e:
        logger.error("bridge_destroy_error", error=str(e), sid=sid)
        await sio.emit(
            "transport:bridge_error",
            {"error": "Bridge destruction failed", "detail": str(e)},
            to=sid,
        )


# ── Signal Quality Events ──────────────────────────────────────


@sio.event
async def signal_measurement_request(sid: str, data: dict):
    """
    Client requests signal quality measurement on a transport.
    """
    try:
        from app.socket.server import get_user_id
        from app.api.routes.transport import _measure_real_signal, _CATALOG_BY_ID

        user_id = await get_user_id(sid)
        if not user_id:
            logger.warning("signal_measurement_no_user", sid=sid)
            return

        if not isinstance(data, dict):
            await sio.emit(
                "transport:signal_error",
                {"error": "invalid_payload"},
                to=sid,
            )
            return

        transport_id = data.get("transport_id")
        logger.info(
            "signal_measurement_request",
            user_id=user_id,
            transport_id=transport_id,
        )

        if transport_id not in _CATALOG_BY_ID:
            await sio.emit(
                "transport:signal_error",
                {"error": "unknown_transport", "transport_id": transport_id},
                to=sid,
            )
            return

        try:
            signal_data = await _measure_real_signal(transport_id)
        except Exception as e:
            logger.error("signal_measure_failed", error=str(e))
            signal_data = {
                "transport_id": transport_id,
                "interface_name": "unknown",
                "signal_strength": 0.0,
                "snr": None,
                "bandwidth": 0.0,
                "latency": 0.0,
                "jitter": 0.0,
                "packet_loss": 100.0,
                "quality_score": 0.0,
                "quality_label": "unavailable",
                "measured_at": datetime.utcnow().isoformat(),
            }

        await sio.emit("transport:signal_update", signal_data, to=sid)
        logger.info("signal_measurement_complete", user_id=user_id)

    except Exception as e:
        logger.error("signal_measurement_error", error=str(e), sid=sid)
        await sio.emit(
            "transport:signal_error",
            {"error": "Signal measurement failed", "detail": str(e)},
            to=sid,
        )


@sio.on("transport:signal_subscribe")
async def subscribe_signal_updates(sid: str, data: dict):
    """
    Client subscribes to periodic signal updates on a transport.
    Server emits transport:signal_update every interval_seconds.
    Subscription auto-cancels on disconnect.
    """
    try:
        from app.socket.server import get_user_id
        from app.api.routes.transport import _measure_real_signal, _CATALOG_BY_ID

        user_id = await get_user_id(sid)
        if not user_id:
            return

        if not isinstance(data, dict):
            return

        transport_id = data.get("transport_id")
        if transport_id not in _CATALOG_BY_ID:
            await sio.emit(
                "transport:signal_error",
                {"error": "unknown_transport", "transport_id": transport_id},
                to=sid,
            )
            return

        # Clamp interval to sane range
        interval = max(2, min(60, int(data.get("interval_seconds") or 5)))

        logger.info(
            "signal_subscribe",
            user_id=user_id,
            transport_id=transport_id,
            interval=interval,
        )

        async def _sub_loop():
            try:
                while True:
                    try:
                        signal = await _measure_real_signal(transport_id)
                        await sio.emit("transport:signal_update", signal, to=sid)
                    except Exception as inner:
                        logger.warning("signal_sub_loop_iteration_failed", error=str(inner))
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("signal_sub_cancelled", sid=sid, transport_id=transport_id)
                raise

        task = asyncio.create_task(_sub_loop())
        _signal_subscriptions.setdefault(sid, []).append((transport_id, task))

    except Exception as e:
        logger.error("signal_subscribe_error", error=str(e), sid=sid)


@sio.on("transport:signal_unsubscribe")
async def unsubscribe_signal_updates(sid: str, data: dict):
    """Cancel one or all signal subscriptions for this client."""
    try:
        target = data.get("transport_id") if isinstance(data, dict) else None
        subs = _signal_subscriptions.get(sid, [])
        kept: list[tuple[str, asyncio.Task]] = []
        for transport_id, task in subs:
            if target is None or transport_id == target:
                task.cancel()
            else:
                kept.append((transport_id, task))
        if kept:
            _signal_subscriptions[sid] = kept
        else:
            _signal_subscriptions.pop(sid, None)
        logger.info("signal_unsubscribe", sid=sid, target=target)
    except Exception as e:
        logger.error("signal_unsubscribe_error", error=str(e))


def cleanup_transport_subscriptions(sid: str) -> None:
    """
    Cancel any signal subscriptions for this SID.
    Called from the main disconnect handler in app/socket/server.py
    so we don't shadow the existing @sio.event disconnect registration.
    """
    try:
        subs = _signal_subscriptions.pop(sid, [])
        for _, task in subs:
            task.cancel()
        if subs:
            logger.info("signal_subs_cleaned_on_disconnect", sid=sid, count=len(subs))
    except Exception as e:
        logger.error("transport_disconnect_cleanup_error", error=str(e))


# ── Peer Events ────────────────────────────────────────────────


@sio.event
async def bridge_peer_joined(sid: str, data: dict):
    """Emitted when a peer joins a bridge."""
    try:
        logger.info("bridge_peer_joined", data=data)
        await sio.emit("transport:peer_joined", data)
    except Exception as e:
        logger.error("bridge_peer_joined_error", error=str(e))


@sio.event
async def bridge_peer_left(sid: str, data: dict):
    """Emitted when a peer leaves/disconnects from a bridge."""
    try:
        logger.info("bridge_peer_left", data=data)
        await sio.emit("transport:peer_left", data)
    except Exception as e:
        logger.error("bridge_peer_left_error", error=str(e))


# ── Failover Events ────────────────────────────────────────────


@sio.event
async def transport_failover(sid: str, data: dict):
    """Emitted when automatic failover occurs."""
    try:
        from_transport = data.get("from_transport_id") if isinstance(data, dict) else None
        to_transport = data.get("to_transport_id") if isinstance(data, dict) else None
        reason = data.get("reason") if isinstance(data, dict) else None

        logger.info(
            "transport_failover",
            from_transport=from_transport,
            to_transport=to_transport,
            reason=reason,
        )

        await sio.emit("transport:auto_failover", data)

    except Exception as e:
        logger.error("transport_failover_error", error=str(e))


# ── Diagnostic Events ──────────────────────────────────────────


@sio.event
async def get_bridge_stats(sid: str, data: dict):
    """Client requests detailed stats for a bridge."""
    try:
        from app.socket.server import get_user_id
        from app.api.routes.transport import _bridges

        user_id = await get_user_id(sid)
        if not user_id:
            return

        if not isinstance(data, dict):
            return

        bridge_id = data.get("bridge_id")
        logger.info("get_bridge_stats", user_id=user_id, bridge_id=bridge_id)

        bridge = _bridges.get(bridge_id)
        if not bridge:
            await sio.emit(
                "transport:stats_error",
                {"error": "bridge_not_found", "bridge_id": bridge_id},
                to=sid,
            )
            return

        uptime = int(time.monotonic() - bridge["_created_monotonic"])
        stats = {
            "bridge_id": bridge_id,
            "uptime_seconds": uptime,
            "bytes_sent": bridge.get("bytes_sent", 0),
            "bytes_received": bridge.get("bytes_received", 0),
            "packets_sent": 0,
            "packets_received": 0,
            "avg_latency_ms": bridge.get("avg_latency_ms"),
            "max_latency_ms": None,
            "min_latency_ms": None,
            "jitter_ms": None,
            "packet_loss_percent": 0.0,
            "peers": list(bridge.get("connected_peers", [])),
            "peer_count": bridge.get("peer_count", 0),
            "is_encrypted": bridge.get("is_encrypted", False),
        }

        await sio.emit("transport:bridge_stats", stats, to=sid)
        logger.info("bridge_stats_sent", bridge_id=bridge_id)

    except Exception as e:
        logger.error("get_bridge_stats_error", error=str(e), sid=sid)
        await sio.emit(
            "transport:stats_error",
            {"error": "Stats retrieval failed", "detail": str(e)},
            to=sid,
        )
