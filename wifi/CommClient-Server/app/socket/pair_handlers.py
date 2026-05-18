"""
Phone-pair Socket.IO handlers.

Design: the phone does NOT join the mediasoup SFU. Instead, a direct WebRTC
P2P connection is established between the phone (browser) and the owner's
desktop client. The server only *relays* SDP/ICE signaling. Once the desktop
has the phone's tracks, it treats them as a local camera/mic source — which
it can then publish into an active call via its existing mediasoup producer
path. No SFU changes required.

Multi-desktop handling — "first-responder wins":
    A user may be logged in on several desktops simultaneously. The phone
    can only speak to one peer at a time, so the server elects a single
    desktop per phone session and locks it in (``_phone_claims``). The
    claim is released when either the phone or the claimed desktop goes
    offline. Only the claimed desktop receives signaling; other desktops
    see "phone online" but never a stream, which prevents multiple
    answers from racing on the phone side.

Events:
  Phone → Server:
    pair:device_online   — phone announces readiness. Server forwards to
                            owner's desktop sockets as pair:phone_ready.
    pair:signal          — phone sends SDP offer/answer or ICE candidate
                            destined for owner's desktop.

  Desktop → Server:
    pair:signal          — desktop sends SDP answer or ICE candidate
                            destined for its paired phone.

  Server → Client (owner's desktop):
    pair:phone_ready     — notifies desktop that a phone has connected.
    pair:signal          — relayed signaling from phone.
    pair:phone_offline   — phone disconnected (or claim released).

  Server → Client (phone):
    pair:signal          — relayed signaling from desktop.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logging import get_logger
from app.socket.server import (
    get_device_type,
    get_remote_addr,
    get_user_id,
    get_sids_for_user,
    sio,
)

# iPhone's Personal Hotspot assigns host .2 and phone .1 inside this /24.
# When the phone socket connects from this subnet we flag the session as
# USB-tethered so desktop clients can show a "via USB" badge.
_TETHER_PREFIX = "172.20.10."


def _classify_transport(remote_addr: str | None) -> str:
    """Return "usb_tether" when the phone's socket IP falls inside the iPhone
    USB hotspot subnet, else "wifi". Falls back to "wifi" when unknown so
    desktop UIs don't have to special-case a third "unknown" state."""
    if not remote_addr:
        return "wifi"
    if remote_addr.startswith(_TETHER_PREFIX):
        return "usb_tether"
    return "wifi"

logger = get_logger(__name__)


# Track online phone sids per user so we can broadcast offline on disconnect.
# user_id → set[sid]
_phone_sids: dict[str, set[str]] = {}

# phone_sid → desktop_sid that owns this pairing session. A phone can only
# speak to one desktop; we elect the first desktop to respond (or the first
# eligible one the phone's broadcast reaches) and pin routing to it.
_phone_claims: dict[str, str] = {}

# phone_sid → session metadata. Populated when a phone announces itself;
# cleared on disconnect. Used by the /pair/sessions REST endpoint so the
# owner's desktop can list/terminate their live phone sessions.
_phone_sessions: dict[str, dict[str, Any]] = {}


def _register_phone(user_id: str, sid: str) -> None:
    _phone_sids.setdefault(user_id, set()).add(sid)


def _unregister_phone(user_id: str, sid: str) -> None:
    sids = _phone_sids.get(user_id)
    if sids:
        sids.discard(sid)
        if not sids:
            _phone_sids.pop(user_id, None)


def _clear_phone_claim(phone_sid: str) -> None:
    _phone_claims.pop(phone_sid, None)


def _clear_claims_by_desktop(desktop_sid: str) -> list[str]:
    """Drop every claim currently held by this desktop sid. Returns the list
    of phone_sids whose claim was released so the caller can notify them."""
    dropped = [p for p, d in _phone_claims.items() if d == desktop_sid]
    for phone_sid in dropped:
        _phone_claims.pop(phone_sid, None)
    return dropped


async def _target_desktop_sids(user_id: str, exclude_sid: str | None = None) -> list[str]:
    """Return desktop (non-phone) sids for a user so signaling reaches only
    the real desktop, not other phones paired to the same account."""
    sids = get_sids_for_user(user_id)
    out: list[str] = []
    for sid in sids:
        if sid == exclude_sid:
            continue
        if (await get_device_type(sid)) != "phone_secondary":
            out.append(sid)
    return out


async def _ensure_phone_claim(user_id: str, phone_sid: str, prefer_sid: str | None = None) -> str | None:
    """Return the desktop_sid that owns this phone's pair session, electing
    one on demand. ``prefer_sid`` lets a desktop claim proactively (e.g. when
    it sends back an answer). Returns ``None`` if no eligible desktop exists."""
    existing = _phone_claims.get(phone_sid)
    live_desktops = await _target_desktop_sids(user_id, exclude_sid=phone_sid)
    live_set = set(live_desktops)

    if existing and existing in live_set:
        return existing
    if existing:
        # Stale claim (desktop went offline): fall through to re-elect.
        _phone_claims.pop(phone_sid, None)

    if prefer_sid and prefer_sid in live_set:
        _phone_claims[phone_sid] = prefer_sid
        return prefer_sid

    if not live_desktops:
        return None
    elected = live_desktops[0]
    _phone_claims[phone_sid] = elected
    return elected


@sio.event
async def pair_device_online(sid: str, data: dict | None = None) -> dict:
    """Phone announces itself. Only accepts from device_type=phone_secondary."""
    user_id = await get_user_id(sid)
    device_type = await get_device_type(sid)
    if not user_id or device_type != "phone_secondary":
        return {"ok": False, "error": "not_authorized"}

    _register_phone(user_id, sid)
    label = (data or {}).get("label") or "Phone camera"
    user_agent = (data or {}).get("user_agent") or ""
    remote_addr = await get_remote_addr(sid)
    transport = _classify_transport(remote_addr)
    _phone_sessions[sid] = {
        "phone_sid": sid,
        "user_id": user_id,
        "label": label,
        "user_agent": user_agent,
        "started_at": time.time(),
        "remote_addr": remote_addr,
        "transport": transport,
    }
    # Structured audit event so operators can see who paired what and when.
    logger.info(
        "pair_session_started",
        user_id=user_id,
        phone_sid=sid,
        label=label,
        user_agent=user_agent,
        transport=transport,
    )

    desktops = await _target_desktop_sids(user_id)
    payload = {
        "phone_sid": sid,
        "user_id": user_id,
        "label": label,
        "transport": transport,
    }
    # Notify every desktop — the UI shows "phone online" everywhere, but only
    # the claimed desktop (elected on first signal) will get the stream.
    for d_sid in desktops:
        try:
            await sio.emit("pair:phone_ready", payload, to=d_sid)
        except Exception as exc:
            logger.warning("pair_phone_ready_emit_failed", sid=d_sid, error=str(exc))
    logger.info("pair_phone_online", user_id=user_id, phone_sid=sid, desktops=len(desktops))
    return {"ok": True, "desktops": len(desktops)}


@sio.event
async def pair_signal(sid: str, data: dict) -> dict:
    """Relay SDP/ICE between phone and its owner's desktop sockets.

    Payload:
      { target_sid?: str, signal: { type: "offer"|"answer"|"ice", ... } }

    If target_sid is absent (phone-initiated), the server routes to the
    claimed desktop — electing one on the first signal. If present
    (desktop-initiated), we verify the sender is the claim-holder (or
    elect them if no claim exists yet) before delivering to the phone.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"ok": False, "error": "not_authenticated"}
    if not isinstance(data, dict) or "signal" not in data:
        return {"ok": False, "error": "bad_payload"}

    device_type = await get_device_type(sid)
    target_sid = data.get("target_sid")
    payload = {
        "from_sid": sid,
        "from_device": device_type,
        "signal": data["signal"],
    }

    if target_sid:
        # Desktop → phone path. Verify target is the same user first.
        target_user = await get_user_id(target_sid)
        if target_user != user_id:
            return {"ok": False, "error": "cross_user_not_allowed"}

        # If the target is a phone, enforce the first-responder-wins claim.
        target_device = await get_device_type(target_sid)
        if target_device == "phone_secondary":
            claimed = await _ensure_phone_claim(user_id, target_sid, prefer_sid=sid)
            if claimed != sid:
                # A different desktop owns this phone. Tell the caller so it
                # can tear down its (stale) PeerConnection instead of waiting
                # on ICE to time out.
                return {"ok": False, "error": "not_claimed", "claimed_by": claimed}

        try:
            await sio.emit("pair:signal", payload, to=target_sid)
        except Exception as exc:
            logger.warning("pair_signal_direct_failed", target_sid=target_sid, error=str(exc))
            return {"ok": False, "error": "emit_failed"}
        return {"ok": True}

    # Phone-initiated without target → route to the claimed desktop only. We
    # re-elect if the previous claim holder went offline so a dropped desktop
    # doesn't strand the phone.
    if device_type == "phone_secondary":
        claimed = await _ensure_phone_claim(user_id, sid)
        if not claimed:
            return {"ok": False, "error": "no_desktop_online"}
        try:
            await sio.emit("pair:signal", payload, to=claimed)
        except Exception as exc:
            logger.warning("pair_signal_emit_failed", target_sid=claimed, error=str(exc))
            return {"ok": False, "error": "emit_failed"}
        return {"ok": True, "delivered": 1, "claimed_desktop": claimed}

    # Non-phone sender without target_sid — shouldn't happen in practice, but
    # we fall back to the legacy broadcast for safety. Exclude the sender.
    desktops = await _target_desktop_sids(user_id, exclude_sid=sid)
    delivered = 0
    for d_sid in desktops:
        try:
            await sio.emit("pair:signal", payload, to=d_sid)
            delivered += 1
        except Exception as exc:
            logger.warning("pair_signal_emit_failed", target_sid=d_sid, error=str(exc))
    return {"ok": True, "delivered": delivered}


async def on_phone_disconnect(sid: str, user_id: str) -> None:
    """Called from the main disconnect handler for phone_secondary sockets."""
    _unregister_phone(user_id, sid)
    _clear_phone_claim(sid)
    session = _phone_sessions.pop(sid, None)
    if session:
        duration = time.time() - session.get("started_at", time.time())
        logger.info(
            "pair_session_ended",
            user_id=user_id,
            phone_sid=sid,
            label=session.get("label"),
            duration_s=round(duration, 1),
        )

    desktops = await _target_desktop_sids(user_id)
    for d_sid in desktops:
        try:
            await sio.emit(
                "pair:phone_offline",
                {"phone_sid": sid, "user_id": user_id},
                to=d_sid,
            )
        except Exception:
            pass


def list_phone_sessions(user_id: str) -> list[dict[str, Any]]:
    """Return a snapshot of every live phone session owned by ``user_id``.
    Safe to call from non-socket contexts (e.g. REST endpoints)."""
    sids = _phone_sids.get(user_id) or set()
    now = time.time()
    out: list[dict[str, Any]] = []
    for s in sids:
        meta = _phone_sessions.get(s) or {"phone_sid": s, "user_id": user_id}
        started = meta.get("started_at", now)
        out.append(
            {
                "phone_sid": s,
                "user_id": user_id,
                "label": meta.get("label") or "Phone camera",
                "user_agent": meta.get("user_agent") or "",
                "started_at": started,
                "duration_s": round(max(0.0, now - started), 1),
                "claimed_by": _phone_claims.get(s),
                "transport": meta.get("transport") or "wifi",
            }
        )
    out.sort(key=lambda r: r["started_at"])
    return out


async def force_disconnect_phone(phone_sid: str, user_id: str) -> bool:
    """Forcefully close a phone socket (used by the /pair/sessions DELETE
    endpoint). Verifies the phone belongs to ``user_id`` before acting.
    Returns True if a matching phone was found and disconnected."""
    sids = _phone_sids.get(user_id) or set()
    if phone_sid not in sids:
        return False
    try:
        # The server's main disconnect handler will fire on_phone_disconnect
        # which clears claims, registers the offline event, and cleans up.
        await sio.disconnect(phone_sid)
    except Exception as exc:
        logger.warning("pair_force_disconnect_failed", phone_sid=phone_sid, error=str(exc))
        return False
    logger.info("pair_session_force_ended", user_id=user_id, phone_sid=phone_sid)
    return True


async def on_desktop_disconnect(sid: str, user_id: str) -> None:
    """Called from the main disconnect handler for desktop sockets. Releases
    any phone claims held by this desktop and nudges other desktops so they
    can try to reclaim if the phone is still online."""
    released = _clear_claims_by_desktop(sid)
    if not released:
        return

    # Re-notify remaining desktops that each phone is "ready" again so their
    # UI can reflect availability. Media won't resume automatically — the
    # phone would need to send a new offer — but the claim slot is now free.
    remaining_desktops = await _target_desktop_sids(user_id, exclude_sid=sid)
    for phone_sid in released:
        if phone_sid not in _phone_sids.get(user_id, set()):
            continue
        for d_sid in remaining_desktops:
            try:
                await sio.emit(
                    "pair:phone_ready",
                    {"phone_sid": phone_sid, "user_id": user_id, "label": "Phone camera"},
                    to=d_sid,
                )
            except Exception:
                pass
    logger.info(
        "pair_desktop_released_claims",
        user_id=user_id,
        desktop_sid=sid,
        phones=len(released),
    )
