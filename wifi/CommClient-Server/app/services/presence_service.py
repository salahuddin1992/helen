"""
Presence service — tracks online/offline status, maps socket IDs to user IDs.
In-memory for performance; status persisted to DB on disconnect.

Thread-safety: All state mutations protected by asyncio.Lock to prevent
race conditions from concurrent socket events.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Valid status values
VALID_STATUSES = frozenset({"online", "offline", "away", "busy", "in_call", "dnd"})


class PresenceService:
    """In-memory presence tracker. Maps user_id <-> socket sid."""

    def __init__(self):
        self._user_sids: dict[str, set[str]] = {}  # user_id -> set of socket sids
        self._sid_user: dict[str, str] = {}  # sid -> user_id
        self._user_status: dict[str, str] = {}  # user_id -> status string
        self._last_heartbeat: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, sid: str) -> None:
        """Register a socket connection for a user (atomic)."""
        was_offline = False
        async with self._lock:
            if user_id not in self._user_sids:
                self._user_sids[user_id] = set()
                was_offline = True
            self._user_sids[user_id].add(sid)
            self._sid_user[sid] = user_id
            # Only set to online if not already in a non-offline status
            # (preserves explicitly-set away/busy/dnd status across reconnections)
            current_status = self._user_status.get(user_id, "offline")
            if current_status == "offline":
                self._user_status[user_id] = "online"
            self._last_heartbeat[user_id] = datetime.now(timezone.utc)
        logger.info("presence_connect", user_id=user_id, sid=sid)
        # Fan out to federation peers only on the 0→online transition, so a
        # user opening a second tab doesn't re-broadcast. Guarded because
        # federation may be disabled or peers may be unreachable.
        #
        # NOTE on DHT STORE: proactive announce-on-connect was removed —
        # bridges between servers should form on-demand only, not before
        # anyone ever asks. The DHT lookup path still works because of:
        #   * route_learned_hint backpropagating after the first chain
        #     delivery (federated_emit.emit_to_user fallback)
        #   * peer_registry.ingest mirroring every learned peer into the
        #     Kademlia routing table
        # If you genuinely need O(1) cross-server lookups from the very
        # first emit, set HELEN_DHT_ACTIVE_ANNOUNCE=1 and the announce
        # task below will fire again.
        if was_offline:
            try:
                from app.core.config import get_settings
                if get_settings().FEDERATION_ENABLED:
                    asyncio.create_task(self._notify_federation_online(user_id))
                    import os as _os_a
                    if _os_a.environ.get("HELEN_DHT_ACTIVE_ANNOUNCE", "").lower() in {"1", "true", "yes", "on"}:
                        asyncio.create_task(self._announce_user_to_dht(user_id))
            except Exception as e:
                logger.debug("federation_presence_online_schedule_failed",
                             user_id=user_id, error=str(e))

    async def _announce_user_to_dht(self, user_id: str) -> None:
        try:
            from app.services.dht_lookup import announce_user_to_dht
            n = await announce_user_to_dht(user_id, ttl_seconds=120.0)
            if n > 0:
                logger.debug("dht_user_announced", user_id=user_id, peers=n)
        except Exception as e:
            logger.debug("dht_announce_failed", user_id=user_id, error=str(e))

    async def _notify_federation_online(self, user_id: str) -> None:
        from app.db.session import async_session_factory
        from app.models.user import User
        from sqlalchemy import select
        from app.services.federated_presence import federated_presence
        try:
            async with async_session_factory() as db:
                row = (await db.execute(
                    select(User).where(User.id == user_id)
                )).scalar_one_or_none()
            if row is None:
                return
            await federated_presence.broadcast_online(
                user_id=user_id,
                username=row.username,
                display_name=row.display_name or row.username,
            )
        except Exception as e:
            logger.debug("federation_presence_online_failed",
                         user_id=user_id, error=str(e))

    async def disconnect(self, sid: str) -> str | None:
        """Unregister a socket connection (atomic). Returns user_id if user went fully offline."""
        async with self._lock:
            user_id = self._sid_user.pop(sid, None)
            if not user_id:
                return None

            sids = self._user_sids.get(user_id, set())
            sids.discard(sid)

            if not sids:
                # No more connections — user is offline
                self._user_sids.pop(user_id, None)
                self._user_status[user_id] = "offline"
                self._last_heartbeat.pop(user_id, None)
                logger.info("presence_disconnect_offline", user_id=user_id)
                try:
                    from app.core.config import get_settings
                    if get_settings().FEDERATION_ENABLED:
                        from app.services.federated_presence import federated_presence
                        asyncio.create_task(federated_presence.broadcast_offline(user_id))
                except Exception as e:
                    logger.debug("federation_presence_offline_schedule_failed",
                                 user_id=user_id, error=str(e))
                return user_id

            logger.info("presence_disconnect_partial", user_id=user_id, remaining=len(sids))
            return None  # Still has other connections

    async def heartbeat(self, user_id: str) -> None:
        async with self._lock:
            self._last_heartbeat[user_id] = datetime.now(timezone.utc)

    async def set_status(self, user_id: str, status: str) -> None:
        """Set user status with validation."""
        async with self._lock:
            if status not in VALID_STATUSES:
                logger.warning("invalid_status_value", user_id=user_id, status=status)
                status = "online"
            self._user_status[user_id] = status

    async def get_status(self, user_id: str) -> str:
        async with self._lock:
            return self._user_status.get(user_id, "offline")

    def get_user_id(self, sid: str) -> str | None:
        return self._sid_user.get(sid)

    def get_sids(self, user_id: str) -> set[str]:
        """Returns a copy of sids to prevent mutation during iteration."""
        return set(self._user_sids.get(user_id, set()))

    async def get_all_online(self) -> dict[str, str]:
        """Returns {user_id: status} for all online users."""
        async with self._lock:
            return {
                uid: status
                for uid, status in self._user_status.items()
                if status != "offline" and uid in self._user_sids
            }

    async def is_online(self, user_id: str) -> bool:
        async with self._lock:
            return user_id in self._user_sids and bool(self._user_sids[user_id])

    def get_online_count(self) -> int:
        """Returns count of online users."""
        return sum(1 for sids in self._user_sids.values() if sids)

    async def get_online_user_ids(self) -> set[str]:
        """Returns set of currently online user IDs (async-safe)."""
        async with self._lock:
            return set(uid for uid, sids in self._user_sids.items() if sids)

    async def get_socket_ids(self, user_id: str) -> set[str]:
        """Returns set of socket IDs for a user (async-safe)."""
        async with self._lock:
            return set(self._user_sids.get(user_id, set()))

    async def cleanup_stale_heartbeats(self, timeout_seconds: int = 60) -> list[str]:
        """
        Remove heartbeat entries for users whose last heartbeat is older
        than timeout AND mark them offline (drop their sids + status).
        Without the second step a network-dead client kept appearing
        "online" forever because its disconnect event never arrived.
        Returns list of user_ids that were cleaned up.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)
            stale_users = []

            for user_id, last_beat in list(self._last_heartbeat.items()):
                age = (now - last_beat).total_seconds()
                if age > timeout_seconds:
                    self._last_heartbeat.pop(user_id, None)
                    # Force-evict every sid still associated with this
                    # user — they're zombies as far as the server can
                    # tell. Reverse-map cleanup keeps _sid_user
                    # consistent so a future reconnect for the same sid
                    # doesn't reuse a stale user binding.
                    sids = self._user_sids.pop(user_id, set())
                    for sid in sids:
                        self._sid_user.pop(sid, None)
                    self._user_status[user_id] = "offline"
                    stale_users.append(user_id)
                    logger.info(
                        "heartbeat_cleanup",
                        user_id=user_id,
                        age_seconds=age,
                        evicted_sids=len(sids),
                    )

            return stale_users


# Singleton
presence_service = PresenceService()
