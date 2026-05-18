"""
LAN-only push notifications — no FCM, no APNs, no third-party.

How it works
------------
On Helen we can't use Firebase / Apple Push because the project is
LAN-only by design. Instead we keep a long-lived WebSocket from
every client (Desktop / Mobile / Web) to the nearest Helen-Router /
Helen-Server, and the server pushes notification frames over that
same socket.

For mobile clients there's an extra wrinkle: Android / iOS aggressively
suspend background apps. We mitigate three ways:

  1. **Foreground service** — the Android side already runs
     ``CallForegroundService.java`` for active calls; this module
     adds ``HelenForegroundService`` for chat too (any user opt-in).
     A persistent low-priority notification keeps the WebSocket alive.
  2. **Periodic wake** — ``NotificationCenter`` records the last-seen
     timestamp per client and, when the user has missed messages for
     more than ``PEEK_INTERVAL_SEC``, queues the notification for
     the *next* foreground tick.
  3. **WoLAN** — for desktop clients on the same wired LAN, we send
     a magic packet (Wake-on-LAN) when a new direct message arrives
     and the user has been offline >5 min. The receiving NIC wakes
     the host, the OS resumes Helen Desktop from tray, the WebSocket
     reconnects, the queued notification is delivered.

This module:

  * Tracks active push subscriptions per (user, device).
  * Fans out incoming events to every live subscription.
  * Queues missed events for offline devices (24-hour TTL).
  * Sends WoL magic packets where the device's MAC is known.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


# ── Subscriptions ───────────────────────────────────────────────────


@dataclass
class PushSubscription:
    user_id: str
    device_id: str
    device_kind: str            # ios | android | windows | linux | macos | web
    socket_id: Optional[str]    # Socket.IO sid if connected
    last_seen_at: float = field(default_factory=time.time)
    mac_address: Optional[str] = None    # for WoLAN
    capabilities: list[str] = field(default_factory=list)
    # ["foreground_service", "wake_on_lan", "silent"]


@dataclass
class PendingNotification:
    notif_id: str
    user_id: str
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    delivered_to: set[str] = field(default_factory=set)


# ── Manager ────────────────────────────────────────────────────────


class LanPushManager:
    """Routes notifications to every live device of a user, queues
    misses, and wakes hosts where possible."""

    QUEUE_TTL_SEC = 24 * 3600
    WOL_OFFLINE_THRESHOLD_SEC = 5 * 60

    def __init__(
        self,
        emit_to_socket: Optional[
            Callable[[str, str, dict], Awaitable[None]]
        ] = None,
    ) -> None:
        self.emit_to_socket = emit_to_socket
        self._subs: dict[tuple[str, str], PushSubscription] = {}
        self._queue: dict[str, list[PendingNotification]] = {}
        self._lock = asyncio.Lock()

    # ── subscription lifecycle ──────────────────────────────

    async def subscribe(self, sub: PushSubscription) -> None:
        async with self._lock:
            self._subs[(sub.user_id, sub.device_id)] = sub
        # Drain the queue for this user/device
        await self._drain_queue(sub.user_id, sub.device_id)

    async def unsubscribe(self, user_id: str, device_id: str) -> None:
        async with self._lock:
            self._subs.pop((user_id, device_id), None)

    async def heartbeat(self, user_id: str, device_id: str) -> None:
        async with self._lock:
            sub = self._subs.get((user_id, device_id))
            if sub:
                sub.last_seen_at = time.time()

    # ── push ────────────────────────────────────────────────

    async def push(self, user_id: str,
                    payload: dict[str, Any]) -> dict[str, list[str]]:
        """Fan out to every live subscription. Returns
        ``{"delivered": [...], "queued": [...]}``."""
        notif_id = payload.get("id") or _short_id()
        delivered: list[str] = []
        queued: list[str] = []

        async with self._lock:
            user_subs = [
                s for (uid, _), s in self._subs.items() if uid == user_id
            ]

        for sub in user_subs:
            if sub.socket_id and self.emit_to_socket:
                try:
                    await self.emit_to_socket(
                        sub.socket_id, "notif:push", payload,
                    )
                    delivered.append(sub.device_id)
                    continue
                except Exception:
                    # fall through to queue
                    pass
            # Subscription is offline — queue + try WoLAN
            await self._enqueue(user_id, notif_id, payload)
            queued.append(sub.device_id)
            await self._maybe_wol(sub)

        if not user_subs:
            await self._enqueue(user_id, notif_id, payload)

        return {"delivered": delivered, "queued": queued}

    # ── queue ───────────────────────────────────────────────

    async def _enqueue(self, user_id: str, notif_id: str,
                        payload: dict[str, Any]) -> None:
        async with self._lock:
            self._queue.setdefault(user_id, []).append(
                PendingNotification(
                    notif_id=notif_id, user_id=user_id, payload=payload,
                ),
            )

    async def _drain_queue(self, user_id: str, device_id: str) -> int:
        sent = 0
        async with self._lock:
            sub = self._subs.get((user_id, device_id))
            pending = self._queue.get(user_id, [])
            now = time.time()
            keep: list[PendingNotification] = []
            for n in pending:
                # TTL check
                if now - n.created_at > self.QUEUE_TTL_SEC:
                    continue
                # Already delivered to this device?
                if device_id in n.delivered_to:
                    keep.append(n)
                    continue
                if sub and sub.socket_id and self.emit_to_socket:
                    try:
                        await self.emit_to_socket(
                            sub.socket_id, "notif:push", n.payload,
                        )
                        n.delivered_to.add(device_id)
                        sent += 1
                    except Exception:
                        keep.append(n)
                        continue
                keep.append(n)
            self._queue[user_id] = keep
        return sent

    # ── Wake-on-LAN ─────────────────────────────────────────

    async def _maybe_wol(self, sub: PushSubscription) -> None:
        if not sub.mac_address:
            return
        if time.time() - sub.last_seen_at < self.WOL_OFFLINE_THRESHOLD_SEC:
            return
        if "wake_on_lan" not in sub.capabilities:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._send_magic_packet, sub.mac_address,
            )
        except Exception:
            pass

    @staticmethod
    def _send_magic_packet(mac: str) -> None:
        """Build and broadcast a Wake-on-LAN magic packet."""
        clean = mac.replace(":", "").replace("-", "").lower()
        if len(clean) != 12:
            return
        try:
            mac_bytes = bytes.fromhex(clean)
        except ValueError:
            return
        packet = b"\xff" * 6 + mac_bytes * 16
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in (7, 9):  # standard WoL ports
                try:
                    s.sendto(packet, ("255.255.255.255", port))
                except OSError:
                    pass
        finally:
            s.close()

    # ── stats ────────────────────────────────────────────────

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "subscriptions": len(self._subs),
                "users_with_queue": len(self._queue),
                "queued_notifications": sum(
                    len(q) for q in self._queue.values()
                ),
            }


def _short_id() -> str:
    import secrets
    return secrets.token_urlsafe(8)


# ── Module-level singleton ──────────────────────────────────────────


_MANAGER: Optional[LanPushManager] = None


def configure_lan_push(
    emit_to_socket: Optional[
        Callable[[str, str, dict], Awaitable[None]]
    ] = None,
) -> LanPushManager:
    """Idempotent — first caller wins."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = LanPushManager(emit_to_socket=emit_to_socket)
    elif emit_to_socket is not None and _MANAGER.emit_to_socket is None:
        _MANAGER.emit_to_socket = emit_to_socket
    return _MANAGER


def get_lan_push() -> Optional[LanPushManager]:
    return _MANAGER
