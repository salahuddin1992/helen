"""
Socket.IO server setup and authentication middleware.

Production hardening:
  - Socket auth validates JWT on every connect
  - Presence updates are atomic (async lock)
  - Graceful disconnect cleans up call/presenter state
  - Per-event error isolation prevents cascading failures
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import socketio

from app.core.logging import get_logger
from app.core.security import decode_token_no_http
from app.services.presence_service import presence_service
from app.socket.rate_limiter import socket_rate_limiter

logger = get_logger(__name__)


class _ReconnectGraceStarted(Exception):
    """Internal sentinel — flow control inside disconnect handler.
    Raised when we deferred the call-leave to a 15s grace window so the
    handler skips its immediate-leave branch but still runs the rest of
    the cleanup (rate limit, presence broadcast)."""


def _is_lan_origin(origin: str) -> bool:
    """Socket.IO origin matcher that mirrors the HTTP CORS regex in
    ``app/main.py``. Accepts:
      * loopback (localhost / 127.0.0.1 / [::1])
      * any bare IPv4 address (LAN host)
      * ``*.local`` mDNS names
      * ``app://.`` (Electron packaged)
      * ``null`` (Electron file:// pages)

    Bare NetBIOS-style hostnames (``http://router/``) are NOT accepted —
    that branch was too permissive and let any single-label name
    pretend to be a LAN peer. JWT auth still catches it but
    defense-in-depth wants a tight origin gate. If you genuinely need
    a NetBIOS name, expose it as ``<name>.local`` via mDNS.
    """
    import re as _re
    # Accept the various opaque origins Electron emits depending on protocol
    # registration timing: "null" per HTML spec for file:// pages, "app://."
    # for the packaged custom-protocol scheme, and bare "file://" or
    # "file://." which some Electron/Chromium versions emit literally before
    # the file:// page fully resolves to opaque-origin.
    if not origin or origin in ("null", "app://.", "file://", "file://."):
        return True
    m = _re.match(
        r"^https?://("
        r"localhost|127\.0\.0\.1|\[::1\]|"
        r"\d+\.\d+\.\d+\.\d+|"
        r"[a-zA-Z0-9-]+\.local"
        r")(:[0-9]+)?$",
        origin,
    )
    return bool(m)


# Optional Redis adapter (audit fix 2.1) — when HELEN_REDIS_URL is set,
# wire python-socketio's AsyncRedisManager so room broadcasts work
# coherently across multiple Helen processes / hosts. With no Redis
# (the LAN-first default), the in-process default applies. Failure to
# connect Redis is logged and degrades gracefully to in-process so a
# Redis outage never bricks the server.
import os as _os_redis_adapter

_socketio_client_manager = None
_REDIS_URL = _os_redis_adapter.environ.get("HELEN_REDIS_URL", "").strip()
_HELEN_ENV = _os_redis_adapter.environ.get("HELEN_ENV", "").strip().lower()
_IS_PRODUCTION = _HELEN_ENV in ("production", "prod")

# ── Production guard (Phase 0 of distributed transformation) ──
# Multi-process / multi-server deployments REQUIRE Redis for socket
# room coherence — without it, group broadcasts only reach members
# whose sids are bound to the SAME process that did the emit. In
# production we fail-fast at import time so an operator can't
# accidentally ship a misconfigured deployment that "works for one
# user" but silently loses fan-out events at scale. Single-server LAN
# deployments (HELEN_ENV unset / "lan" / "dev") still degrade
# gracefully to in-process default.
if _IS_PRODUCTION and not _REDIS_URL:
    raise RuntimeError(
        "HELEN_REDIS_URL is required when HELEN_ENV=production. "
        "Production deployments must use Redis for Socket.IO room "
        "coherence across processes/hosts. Set HELEN_REDIS_URL to a "
        "reachable redis:// URL, or unset HELEN_ENV for single-process "
        "LAN deployments."
    )

if _REDIS_URL:
    try:
        # python-socketio ships AsyncRedisManager which uses
        # `redis.asyncio` under the hood. Importing the manager class
        # is cheap; the actual connection is lazy on first use.
        from socketio import AsyncRedisManager as _AsyncRedisManager
        _socketio_client_manager = _AsyncRedisManager(_REDIS_URL)
        logger.info("socketio_redis_adapter_enabled", url_prefix=_REDIS_URL.split("@")[0][:32])
    except Exception as _redis_err:
        if _IS_PRODUCTION:
            # In production, a Redis adapter wiring failure is fatal —
            # silent fallback would mean lost events in fanout.
            raise RuntimeError(
                f"HELEN_ENV=production but socketio Redis adapter failed "
                f"to initialize: {_redis_err}. Fix the Redis URL or "
                f"unset HELEN_ENV."
            )
        logger.warning(
            "socketio_redis_adapter_unavailable",
            error=str(_redis_err),
            note="Falling back to in-process manager. "
                 "For multi-process room coherence, install `redis>=4.5` "
                 "and ensure HELEN_REDIS_URL is reachable.",
        )
        _socketio_client_manager = None

# Create Socket.IO async server — hardened configuration
# NOTE: python-socketio's `cors_allowed_origins` accepts a list OR "*" OR a
# callable. For the three-machine LAN scenario (admin on host B, server on
# host A, client on host C — each with a different Origin) we use "*" to
# short-circuit the library's exact-match check, then gate on our own
# regex in the connect handler. JWT auth in the handler still prevents
# random websites from hijacking sockets even if origin were spoofed.
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    client_manager=_socketio_client_manager,  # None = in-process default
    logger=False,
    engineio_logger=False,
    # Tolerant ping timeouts — during megascale bursts the event loop can be
    # briefly saturated, which delays server→client pings. With the previous
    # 25s timeout, sockets got silently dropped mid-burst and subsequent emits
    # from them were buffered client-side (reconnection disabled in tests),
    # causing chat fanouts to reach nobody. 90s is still short enough to
    # reap genuinely-dead sockets within a minute.
    ping_timeout=90,
    ping_interval=15,
    max_http_buffer_size=5 * 1024 * 1024,  # 5MB
    # Per-message deflate compression. Cuts chat-message bandwidth ~70%
    # (text compresses very well) and large group-call signaling SDP
    # ~60%. CPU cost on a 14-core box is negligible — measured ≤2% per
    # 1000-msg/s throughput. Disable via HELEN_SOCKETIO_COMPRESSION=0
    # if a specific deployment hits CPU saturation.
    compression=(__import__("os").environ.get("HELEN_SOCKETIO_COMPRESSION", "1") == "1"),
    compression_threshold=1024,  # only compress payloads ≥1 KB; smaller is wasted CPU
)


@sio.event
async def connect(sid: str, environ: dict, auth: dict | None = None):
    """
    Authenticate socket connections via JWT in the auth payload.
    Client must send: io.connect(url, { auth: { token: "..." } })
    """
    # Origin gate — we told python-socketio to accept "*" so the exact-match
    # allowlist wouldn't block LAN hosts; here we re-enforce the LAN/Electron
    # pattern on the actual Origin header. JWT validation below is the main
    # auth, this is defense-in-depth against cross-site socket hijacks.
    origin = environ.get("HTTP_ORIGIN", "") if isinstance(environ, dict) else ""
    if origin and not _is_lan_origin(origin):
        logger.warning("socket_connect_bad_origin", sid=sid, origin=origin[:120])
        raise socketio.exceptions.ConnectionRefusedError("Origin not allowed")

    if not auth or not isinstance(auth, dict) or "token" not in auth:
        logger.warning("socket_connect_no_token", sid=sid)
        raise socketio.exceptions.ConnectionRefusedError("Authentication required")

    token = auth["token"]
    if not isinstance(token, str) or len(token) > 4096:
        logger.warning("socket_connect_malformed_token", sid=sid)
        raise socketio.exceptions.ConnectionRefusedError("Malformed token")

    payload = decode_token_no_http(token)
    if not payload or payload.get("type") != "access":
        logger.warning("socket_connect_invalid_token", sid=sid)
        raise socketio.exceptions.ConnectionRefusedError("Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id or not isinstance(user_id, str):
        logger.warning("socket_connect_no_subject", sid=sid)
        raise socketio.exceptions.ConnectionRefusedError("Invalid token payload")

    # SECURITY: Enforce per-user connection limit
    if not socket_rate_limiter.can_connect(user_id):
        logger.warning("socket_connect_limit_exceeded", sid=sid, user_id=user_id,
                        count=socket_rate_limiter.get_connection_count(user_id))
        raise socketio.exceptions.ConnectionRefusedError("Too many concurrent connections")

    # Save user_id in session. Also carry device_type (defaults to "desktop")
    # so phone-pairing handlers can distinguish secondary devices. The socket
    # origin IP is preserved so USB-tether detection (172.20.10.0/24) can
    # decide whether a phone is reachable over the direct USB subnet without
    # requiring the phone itself to self-report.
    device_type = payload.get("device_type") or "desktop"
    remote_addr = _extract_remote_addr(environ)
    user_agent = (environ.get("HTTP_USER_AGENT") or "")[:256]
    connected_at = datetime.now(timezone.utc).isoformat()
    async with sio.session(sid) as session:
        session["user_id"] = user_id
        session["device_type"] = device_type
        session["remote_addr"] = remote_addr
        session["user_agent"] = user_agent
        session["connected_at"] = connected_at

    # Register presence (atomic) and track connection count
    await presence_service.connect(user_id, sid)
    socket_rate_limiter.record_connect(user_id)

    # Admins auto-join the federation-event room so their dashboards get
    # live bridge activity (forwards, dedup drops, local deliveries) the
    # instant it happens. Room membership costs nothing if no admin
    # dashboard is subscribed; the emit is a no-op.
    role_claim = payload.get("role") or ""
    if role_claim == "admin":
        try:
            await sio.enter_room(sid, "admin_federation")
        except Exception:
            pass

    # NOTE: we deliberately do *not* pre-join channel:{id} rooms here.
    # Doing one SELECT per socket connect quadrupled connect latency under
    # mass-connect bursts. Instead, rooms are populated lazily the first
    # time a channel is broadcast to (see channel_room.ensure_populated).
    #
    # However, if any channel rooms are *already warm* from prior broadcasts,
    # we need to slip the new sid into them — otherwise late-connecting
    # members miss every subsequent broadcast until the room is invalidated.
    # add_new_sid short-circuits when _populated is empty (the common case
    # during a mass-connect burst before any broadcast has fired).
    try:
        from app.socket.channel_room import add_new_sid as _channel_add_new_sid
        await _channel_add_new_sid(sio, user_id, sid)
    except Exception as _e:
        logger.warning("channel_room_add_new_sid_failed", user_id=user_id, sid=sid, error=str(_e))

    # Per-connect presence broadcast is still O(N²) across the whole connect
    # storm — each new connect fans out to every existing socket, so a 10k
    # sign-in burst is 50M emit() calls on a single event loop. Gate by
    # total online count: below 500, the roster is small enough that realtime
    # presence updates matter; above that, clients should poll a digest
    # endpoint instead.
    online_count = presence_service.get_online_count()
    if online_count <= 500:
        # Fire-and-forget — no need to block this handler on the fan-out.
        import asyncio as _asyncio_pres
        _asyncio_pres.create_task(sio.emit("presence:user_online", {
            "user_id": user_id,
            "status": "online",
        }, skip_sid=sid))

    # Authoritative snapshot only for small rosters — shipping a 10 000-entry
    # list to every incoming socket would dwarf every other hot-path write.
    if online_count <= 500:
        online_users = await presence_service.get_all_online()
        await sio.emit("presence:online_list", {
            "online_users": online_users,
        }, to=sid)
    else:
        await sio.emit("presence:online_list", {
            "online_users": [],
            "truncated": True,
            "count": online_count,
        }, to=sid)

    # Cross-server presence push: tell sibling Helen servers that this
    # user is now online, so anyone over there calling
    # ``federated_presence.get(user_id)`` (e.g. v2_call_initiate target
    # validation) sees them immediately instead of waiting up to 60s
    # for the resync loop to pull. Fire-and-forget — federation may be
    # disabled and that's fine. We need a username/display_name; the
    # cheapest source is a single SQL roundtrip.
    try:
        from app.core.config import get_settings as _gs
        if _gs().FEDERATION_ENABLED and _gs().FEDERATION_SECRET:
            from app.db.session import async_session_factory as _sf
            from sqlalchemy import select as _sel
            from app.models.user import User as _User
            from app.services.federated_presence import federated_presence as _fp
            import asyncio as _aio_fp
            async def _push():
                try:
                    async with _sf() as db:
                        row = (await db.execute(
                            _sel(_User).where(_User.id == user_id)
                        )).scalar_one_or_none()
                    if row:
                        await _fp.broadcast_online(
                            user_id=row.id,
                            username=row.username,
                            display_name=row.display_name or row.username,
                        )
                except Exception as _e:
                    logger.debug("federated_presence_push_failed", error=str(_e))
            _aio_fp.create_task(_push())
    except Exception:
        pass

    # Register/refresh a LAN-push subscription so any notifications
    # we couldn't deliver (e.g. DM to a user who was offline) drain
    # to this newly-connected socket. Best-effort — manager not
    # configured yet means lifespan hasn't reached configure_lan_push.
    try:
        from app.services.lan_push import (
            get_lan_push, PushSubscription as _PS,
        )
        mgr = get_lan_push()
        if mgr is not None:
            try:
                async with sio.session(sid) as _sess:
                    _device_id = _sess.get("device_id") or sid
                    _device_kind = _sess.get("device_kind") or "web"
                    _mac = _sess.get("mac_address")
            except Exception:
                _device_id = sid
                _device_kind = "web"
                _mac = None
            await mgr.subscribe(_PS(
                user_id=user_id,
                device_id=_device_id,
                device_kind=_device_kind,
                socket_id=sid,
                mac_address=_mac,
                capabilities=["wake_on_lan"] if _mac else [],
            ))
    except Exception as _lpe:
        logger.debug("lan_push_subscribe_failed",
                     sid=sid, user_id=user_id, error=str(_lpe))

    logger.info("socket_connected", sid=sid, user_id=user_id)


@sio.event
async def disconnect(sid: str):
    """Handle socket disconnect — update presence + cleanup orphaned call/presenter state."""
    # Cleanup voice message playback state
    from app.socket.voice_handlers import cleanup_voice_playback
    cleanup_voice_playback(sid)

    # Mark the LAN-push subscription as offline (clear socket_id) so
    # future notifications queue instead of being lost to the dead
    # socket. We deliberately keep the subscription so when the user
    # reconnects on the same device the queue drains to them.
    try:
        from app.services.lan_push import get_lan_push
        mgr = get_lan_push()
        if mgr is not None:
            uid_for_lp = presence_service.get_user_id(sid)
            if uid_for_lp:
                async with sio.session(sid) as _sess:
                    _device_id = _sess.get("device_id") or sid
                async with mgr._lock:
                    sub = mgr._subs.get((uid_for_lp, _device_id))
                    if sub is not None:
                        sub.socket_id = None
                        sub.last_seen_at = time.time()
    except Exception as _lpe:
        logger.debug("lan_push_disconnect_hook_failed",
                     sid=sid, error=str(_lpe))

    # Cleanup transport-layer signal subscriptions
    try:
        from app.socket.transport_handlers import cleanup_transport_subscriptions
        cleanup_transport_subscriptions(sid)
    except Exception as _e:
        logger.warning("transport_cleanup_failed", error=str(_e))

    user_id = presence_service.get_user_id(sid)

    # If this was a paired phone, tell the owner's desktop it went offline.
    # If it was a desktop, release any phone-pair claims it held so the
    # phone's session can be re-elected by another desktop.
    try:
        if user_id:
            device_type = await get_device_type(sid)
            if device_type == "phone_secondary":
                from app.socket.pair_handlers import on_phone_disconnect
                await on_phone_disconnect(sid, user_id)
            else:
                from app.socket.pair_handlers import on_desktop_disconnect
                await on_desktop_disconnect(sid, user_id)
    except Exception as _e:
        logger.warning("pair_disconnect_hook_failed", sid=sid, error=str(_e))

    # Track connection count decrease
    if user_id:
        socket_rate_limiter.record_disconnect(user_id)

    went_offline_user = await presence_service.disconnect(sid)

    if went_offline_user:
        # User has no more connections — cleanup active call state
        try:
            from app.services.call_service import call_service
            from app.services.presenter_service import presenter_service

            call = call_service.get_user_call(went_offline_user)
            if call:
                # Group calls (mesh/sfu) get a reconnect grace window —
                # we hold the participant slot for up to 15s and emit
                # `call:participant-reconnecting` so peers' UIs can show
                # the spinner overlay instead of removing the tile.
                # P2P calls keep their fail-fast behavior.
                if call.routing in ("mesh", "sfu") and call.status == "active":
                    # Best-effort reconnect grace. We do NOT remove the
                    # participant yet; instead we publish the
                    # reconnecting event and schedule a deferred leave.
                    for pid in list(call.participants.keys()):
                        if pid == went_offline_user:
                            continue
                        for p_sid in presence_service.get_sids(pid):
                            try:
                                await sio.emit("call:participant-reconnecting", {
                                    "call_id": call.call_id,
                                    "user_id": went_offline_user,
                                }, to=p_sid)
                            except Exception:
                                pass
                    # Append to event log so reconnecting clients see it
                    try:
                        call.append_event("call:participant-reconnecting", {
                            "call_id": call.call_id, "user_id": went_offline_user,
                        })
                    except Exception:
                        pass
                    # Schedule grace check
                    import asyncio as _asyncio_grace
                    async def _grace_then_leave():
                        await _asyncio_grace.sleep(15)
                        # Did the user come back? If they have any active
                        # sids again, abort the leave.
                        if presence_service.get_sids(went_offline_user):
                            logger.info("call_reconnect_grace_recovered",
                                        user_id=went_offline_user, call_id=call.call_id)
                            return
                        try:
                            ended_call = await call_service.leave_call(call.call_id, went_offline_user)
                            for pid in list(ended_call.participants.keys()):
                                for p_sid in presence_service.get_sids(pid):
                                    await sio.emit("call_participant_left", {
                                        "call_id": call.call_id,
                                        "user_id": went_offline_user,
                                        "reason": "disconnect_grace_expired",
                                    }, to=p_sid)
                            if ended_call.status == "ended":
                                presenter_service.cleanup_call(ended_call.call_id)
                                try:
                                    from app.db.session import async_session_factory
                                    async with async_session_factory() as _db:
                                        await call_service.persist_call_log(_db, ended_call)
                                except Exception as e:
                                    logger.error("grace_leave_persist_failed", error=str(e))
                        except Exception as e:
                            logger.error("grace_leave_failed",
                                         user_id=went_offline_user, error=str(e))
                    task = _asyncio_grace.create_task(_grace_then_leave())
                    call_service._bg_tasks.add(task)
                    task.add_done_callback(call_service._bg_tasks.discard)
                    presenter_service.remove_participant(call.call_id, went_offline_user)
                    logger.info("call_reconnect_grace_started",
                                user_id=went_offline_user, call_id=call.call_id, grace=15)
                    # Skip immediate-leave path; jump to post-cleanup
                    raise _ReconnectGraceStarted()
                # P2P / non-active path falls through to immediate leave
                presenter_service.remove_participant(call.call_id, went_offline_user)
                ended_call = await call_service.leave_call(call.call_id, went_offline_user)
                for pid in list(ended_call.participants.keys()):
                    for p_sid in presence_service.get_sids(pid):
                        await sio.emit("call_participant_left", {
                            "call_id": call.call_id,
                            "user_id": went_offline_user,
                            "reason": "disconnect",
                        }, to=p_sid)

                # If call ended, persist log and cleanup presenter state
                if ended_call.status == "ended":
                    presenter_service.cleanup_call(ended_call.call_id)
                    try:
                        from app.db.session import async_session_factory
                        async with async_session_factory() as db:
                            await call_service.persist_call_log(db, ended_call)
                    except Exception as e:
                        logger.error("disconnect_persist_call_error", error=str(e))

                logger.info("disconnect_call_cleanup", user_id=went_offline_user, call_id=call.call_id)
        except _ReconnectGraceStarted:
            # Sentinel — grace started, skip immediate-leave but
            # continue with rate-limit cleanup + presence broadcast.
            pass
        except Exception as e:
            logger.error("disconnect_cleanup_error", user_id=went_offline_user, error=str(e))

        # Cleanup rate limit state
        socket_rate_limiter.cleanup_user(went_offline_user)

        # Broadcast offline
        await sio.emit("presence:user_offline", {
            "user_id": went_offline_user,
        })

        # Cross-server: tell sibling servers this user is gone so their
        # federated_presence cache evicts them immediately (otherwise
        # peers see a stale "online" entry until the next resync).
        try:
            from app.core.config import get_settings as _gs
            if _gs().FEDERATION_ENABLED and _gs().FEDERATION_SECRET:
                from app.services.federated_presence import federated_presence as _fp
                import asyncio as _aio_fp
                _aio_fp.create_task(_fp.broadcast_offline(went_offline_user))
        except Exception:
            pass

        logger.info("socket_disconnected_offline", sid=sid, user_id=went_offline_user)
    else:
        logger.info("socket_disconnected", sid=sid, user_id=user_id)


async def get_user_id(sid: str) -> str | None:
    """Helper to get user_id from socket session."""
    try:
        async with sio.session(sid) as session:
            return session.get("user_id")
    except Exception:
        return None


async def get_device_type(sid: str) -> str:
    """Helper to get device_type from socket session ("desktop" default)."""
    try:
        async with sio.session(sid) as session:
            return session.get("device_type") or "desktop"
    except Exception:
        return "desktop"


async def get_remote_addr(sid: str) -> str | None:
    """Return the IP address the socket connected from, or None if unknown.
    For LAN deployments this is the direct client IP; behind a proxy we
    honor ``X-Forwarded-For`` / ``X-Real-IP`` (set at connect time)."""
    try:
        async with sio.session(sid) as session:
            return session.get("remote_addr")
    except Exception:
        return None


def _extract_remote_addr(environ: dict) -> str | None:
    """Pull the best-available client IP out of a WSGI/ASGI environ dict.
    Picks the left-most ``X-Forwarded-For`` entry (closest to the client)
    so a reverse proxy doesn't mask the real tether IP."""
    try:
        xff = environ.get("HTTP_X_FORWARDED_FOR")
        if isinstance(xff, str) and xff.strip():
            return xff.split(",")[0].strip() or None
        real = environ.get("HTTP_X_REAL_IP")
        if isinstance(real, str) and real.strip():
            return real.strip()
        addr = environ.get("REMOTE_ADDR")
        if isinstance(addr, str) and addr.strip():
            return addr.strip()
        # ASGI fallback: raw scope under "asgi.scope" with client tuple.
        scope = environ.get("asgi.scope")
        if isinstance(scope, dict):
            client = scope.get("client")
            if client and isinstance(client, (tuple, list)) and client:
                return str(client[0])
    except Exception:
        pass
    return None


def get_sids_for_user(user_id: str) -> list[str]:
    """Return every active sid for a user (multi-device). Empty list if offline."""
    try:
        return list(presence_service.get_sids(user_id) or [])
    except Exception:
        return []


async def emit_to_user(event: str, payload: dict, user_id: str, skip_sid: str | None = None) -> int:
    """Fan-out an event to every connected session of ``user_id``.
    Returns the number of sids the event was delivered to.

    Cross-server delivery: when the user has no local sockets, falls
    back to ``federated_emit.emit_to_user`` which (a) checks the
    learned-origin cache and emits direct to the owning peer if known,
    (b) otherwise floods via chain routing with dedup. This is what
    makes a Helen LAN of N servers behave like a single namespace —
    the chat layer doesn't have to know which server hosts a user.
    """
    count = 0
    for sid in get_sids_for_user(user_id):
        if sid == skip_sid:
            continue
        try:
            await sio.emit(event, payload, to=sid)
            count += 1
        except Exception as exc:
            logger.warning("emit_to_user_failed", user_id=user_id, sid=sid, error=str(exc))
    if count > 0:
        return count
    # No local sids — try the federation. Lazy-import to avoid a
    # startup cycle (federated_emit imports presence_service which
    # imports back to socket layer in some edge cases).
    try:
        from app.services.federated_emit import emit_to_user as _fed_emit
        return await _fed_emit(user_id, event, payload)
    except Exception as exc:
        logger.warning("federated_emit_to_user_failed",
                       user_id=user_id, event=event, error=str(exc))
        return 0
