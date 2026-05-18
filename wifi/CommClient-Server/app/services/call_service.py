"""
Call signaling service — manages call lifecycle and state.
In-memory call state (not persisted mid-call). Logs to DB when call ends.

Production hardening:
  - asyncio.Lock on all state mutations
  - Call TTL cleanup (orphan detection)
  - Max participants enforcement
  - Media type validation
  - Presenter state cleanup on call end
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.call_log import CallLog

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────
VALID_MEDIA_TYPES = frozenset({"audio", "video"})
VALID_ROUTING_TYPES = frozenset({"p2p", "mesh", "sfu", "hybrid"})
# Hybrid topology manager switches mesh→SFU above MESH_MAX_PARTICIPANTS
# so clients no longer carry O(n²) peer connections.
# The hard ceiling here is a SERVER-side guard against resource
# exhaustion. The previous 1_000_000 was unrealistic — mediasoup
# breaks down well before that. 2000 is the new default for LAN deployments
# (auditorium / classroom broadcast scenarios with one speaker + N listeners
# riding the SFU broadcast subscription budget). Operators running mesh-only
# small calls can lower this; large LAN broadcasts can raise it. Hard floor 8
# prevents misconfig from disabling group calls entirely.
import os as _os_max
try:
    _env_max = int(_os_max.environ.get("HELEN_MAX_CALL_PARTICIPANTS", "2000"))
except ValueError:
    _env_max = 2000
MAX_CALL_PARTICIPANTS = max(8, _env_max)
CALL_RINGING_TIMEOUT_SEC = 60  # Orphan detection: ringing for >60s
CALL_MAX_DURATION_SEC = 14400  # 4 hours max


class ActiveCall:
    """In-memory representation of an active call."""

    def __init__(
        self,
        call_id: str,
        initiator_id: str,
        call_type: str,  # "audio" | "video"
        routing: str,  # "p2p" | "sfu" | "mesh"
        channel_id: str | None = None,
    ):
        self.call_id = call_id
        self.initiator_id = initiator_id
        self.call_type = call_type
        self.routing = routing
        self.channel_id = channel_id
        self.status = "ringing"  # ringing, active, ended
        self.participants: dict[str, dict[str, Any]] = {}  # user_id -> {joined_at, muted, video_off, sharing_screen}
        self.created_at = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        # Call hold/resume feature
        self.on_hold_users: set[str] = set()
        # Call quality tracking
        self.quality_reports: dict[str, list] = {}
        # Track max participants ever reached (for accurate call log persistence)
        self._max_participants: int = 0
        # One-shot guard for call_log persistence — without it, every
        # disconnecting socket in a large call would race to INSERT the
        # same end-of-call log row and pile up against the SQLite lock.
        self._log_persisted: bool = False
        # ── Event replay log ─────────────────────────────────────
        # Bounded log of state-change events for missed-event replay
        # on reconnect. Each event has a monotonic `seq` so the client
        # can reconnect with the last seq it saw and the server returns
        # everything after. Uses deque(maxlen=N) for O(1) eviction
        # instead of the previous list-slice pattern, which copied
        # 800 entries every overflow. Cap is env-tunable; the default
        # was lowered from 1000 → 500 since 1000 calls × 1000 events
        # = 1M objects under sustained heavy load.
        import os as _os_call_cap
        try:
            _events_max = int(_os_call_cap.environ.get("HELEN_CALL_EVENT_LOG_MAX", "500"))
        except ValueError:
            _events_max = 500
        from collections import deque as _deque
        self.events: "_deque[dict[str, Any]]" = _deque(maxlen=max(50, _events_max))
        self._sequence: int = 0

    def append_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a state-change event to the replay log."""
        self._sequence += 1
        entry = {
            "seq": self._sequence,
            "type": event_type,
            "payload": payload,
            "ts": datetime.now(timezone.utc).timestamp(),
        }
        self.events.append(entry)  # deque(maxlen=…) auto-evicts oldest
        return entry

    @property
    def current_sequence(self) -> int:
        return self._sequence

    def events_since(self, last_seq: int, limit: int = 500) -> list[dict[str, Any]]:
        return [e for e in self.events if e["seq"] > last_seq][:limit]

    def add_participant(self, user_id: str) -> None:
        # Idempotent — re-adding a present participant is a no-op so
        # duplicate accept events don't double-record in the event log.
        if user_id in self.participants:
            return
        now = datetime.now(timezone.utc)
        self.participants[user_id] = {
            "joined_at": now,
            "last_active_at": now,
            "muted": False,
            "video_off": False,
            "sharing_screen": False,
        }
        if len(self.participants) > self._max_participants:
            self._max_participants = len(self.participants)
        # Replay-log entry — drives v2_call_reconnect missed_events
        self.append_event("call:participant-joined", {
            "call_id": self.call_id,
            "user_id": user_id,
        })

    def remove_participant(self, user_id: str) -> None:
        if user_id not in self.participants:
            return
        self.participants.pop(user_id, None)
        self.append_event("call:participant-left", {
            "call_id": self.call_id,
            "user_id": user_id,
        })

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "initiator_id": self.initiator_id,
            "call_type": self.call_type,
            "routing": self.routing,
            "channel_id": self.channel_id,
            "status": self.status,
            "participants": {
                uid: {
                    "muted": p["muted"],
                    "video_off": p["video_off"],
                    "sharing_screen": p["sharing_screen"],
                }
                for uid, p in self.participants.items()
            },
            "participant_count": len(self.participants),
            "created_at": self.created_at.isoformat(),
        }


class CallService:
    """Manages active calls in-memory and persists call logs to DB."""

    def __init__(self):
        self._active_calls: dict[str, ActiveCall] = {}  # call_id -> ActiveCall
        self._user_calls: dict[str, str] = {}  # user_id -> call_id (one call at a time)
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._initiate_timestamps: dict[str, deque[float]] = {}  # user_id -> deque(maxlen=20) of call initiation timestamps
        # Background tasks spawned from sync cleanup paths — hard refs so the
        # event loop does not GC them mid-flight. Tasks self-deregister on done.
        self._bg_tasks: set[asyncio.Task] = set()

    def start_cleanup_loop(self) -> None:
        """Start periodic orphan call cleanup (call from app startup)."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._orphan_cleanup_loop())

    async def shutdown(self) -> None:
        """Gracefully shutdown the call service. Cancels cleanup loop and
        drains any in-flight topology-release / background tasks so the
        mediasoup worker does not leak routers on deploy."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Drain background tasks (topology releases, etc.) with a short grace
        pending = [t for t in self._bg_tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("call_service_bg_drain_timeout", pending=len(pending))

        logger.info("call_service_shutdown")

    # Per-participant idle eviction: if a peer hasn't pinged a
    # heartbeat in this many seconds, remove them from the call so
    # quotas free up + topology can downsize. 45s is conservative
    # — clients heartbeat every 10s; a missed 4 pings means the
    # connection is genuinely gone. Without this, dead-tab clients
    # eat slot capacity until the call hits max-duration.
    PARTICIPANT_IDLE_EVICT_SEC = 45

    async def _orphan_cleanup_loop(self) -> None:
        """Sweep orphaned calls + evict idle participants.

        Runs every 10s (down from 30s) so dead participants free up
        slot quota and topology can downsize within seconds rather
        than minutes. Protected by lock.
        """
        while True:
            try:
                await asyncio.sleep(10)  # Tighter cadence for big calls
                now = datetime.now(timezone.utc)
                orphans: list[str] = []
                idle_evictions: list[tuple[str, str]] = []  # (call_id, user_id)

                # Hold lock while detecting orphans + idle peers
                async with self._lock:
                    for call_id, call in list(self._active_calls.items()):
                        # Empty participants is an orphan regardless of
                        # status — a "ringing" call whose initiator
                        # disconnected before anyone joined still has
                        # status="ringing" but no participants and
                        # should be reaped immediately rather than
                        # waiting for the ringing-timeout grace period.
                        if len(call.participants) == 0:
                            orphans.append(call_id)
                            continue
                        # Ringing too long
                        if call.status == "ringing":
                            elapsed = (now - call.created_at).total_seconds()
                            if elapsed > CALL_RINGING_TIMEOUT_SEC:
                                orphans.append(call_id)
                        # Active too long
                        elif call.status == "active" and call.started_at:
                            elapsed = (now - call.started_at).total_seconds()
                            if elapsed > CALL_MAX_DURATION_SEC:
                                orphans.append(call_id)
                                continue

                        # Idle-peer eviction inside otherwise-healthy
                        # active calls. Skip ringing calls — joining
                        # peers haven't started heartbeating yet.
                        if call.status == "active":
                            for uid, p in list(call.participants.items()):
                                last_active = p.get("last_active_at") or p.get("joined_at")
                                if not last_active:
                                    continue
                                idle_for = (now - last_active).total_seconds()
                                if idle_for > self.PARTICIPANT_IDLE_EVICT_SEC:
                                    idle_evictions.append((call_id, uid))

                    for call_id in orphans:
                        call = self._active_calls.get(call_id)
                        if call:
                            call.status = "ended"
                            call.ended_at = now
                            self._cleanup_call(call)
                            logger.warning("orphan_call_cleaned", call_id=call_id)

                    # Apply idle evictions inside the same lock so we
                    # don't race with leave_call.
                    for call_id, uid in idle_evictions:
                        call = self._active_calls.get(call_id)
                        if not call or uid not in call.participants:
                            continue
                        call.remove_participant(uid)
                        self._user_calls.pop(uid, None)
                        logger.info(
                            "idle_participant_evicted",
                            call_id=call_id, user_id=uid,
                            idle_seconds=self.PARTICIPANT_IDLE_EVICT_SEC,
                        )

                # Best-effort follow-up: notify rooms about idle
                # evictions so other participants drop the dead tile.
                for call_id, uid in idle_evictions:
                    try:
                        from app.socket.server import sio as _sio_idle
                        await _sio_idle.emit(
                            "call:participant_left",
                            {"call_id": call_id, "user_id": uid, "reason": "idle"},
                            room=f"call:{call_id}",
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("orphan_cleanup_error", error=str(e))

    async def initiate_call(
        self,
        initiator_id: str,
        call_type: str,
        routing: str = "p2p",
        channel_id: str | None = None,
    ) -> ActiveCall:
        """Create a new call. Uses async lock to prevent race conditions."""
        # Validate inputs before acquiring lock
        if call_type not in VALID_MEDIA_TYPES:
            raise ValueError(f"Invalid media type: {call_type}. Must be one of: {VALID_MEDIA_TYPES}")
        if routing not in VALID_ROUTING_TYPES:
            raise ValueError(f"Invalid routing: {routing}. Must be one of: {VALID_ROUTING_TYPES}")

        async with self._lock:
            if initiator_id in self._user_calls:
                raise ValueError("User already in a call")

            call_id = uuid.uuid4().hex
            call = ActiveCall(
                call_id=call_id,
                initiator_id=initiator_id,
                call_type=call_type,
                routing=routing,
                channel_id=channel_id,
            )
            call.add_participant(initiator_id)
            self._active_calls[call_id] = call
            self._user_calls[initiator_id] = call_id

        # ── Persist to DB (multi-worker + restart recovery) ───────────────
        try:
            from app.services.call_state_persistence import call_state_persistence
            await call_state_persistence.upsert_call(
                call_id=call_id,
                initiator_id=initiator_id,
                call_type=call_type,
                routing=routing,
                channel_id=channel_id,
                status="ringing",
                max_participants=1,
                topology_generation=1,
            )
            await call_state_persistence.add_participant(
                call_id=call_id, user_id=initiator_id, sid=None, role="initiator",
            )
        except Exception as exc:
            logger.error("call_persist_initiate_failed", call_id=call_id, error=str(exc))

        # Mirror to distributed_group_call_state so sibling servers
        # can read participant data without round-tripping us. Best-
        # effort; failures don't block the call setup.
        try:
            from app.services.distributed_group_call_state import (
                get_group_call_state, GroupCallMeta,
            )
            gcs = get_group_call_state()
            await gcs.set_meta(call_id, GroupCallMeta(
                channel_id=channel_id, call_type=call_type, routing=routing,
            ))
            await gcs.add_participant(
                call_id, initiator_id, role="host",
            )
        except Exception as exc:
            logger.warning("gcs_initiate_mirror_failed", call_id=call_id, error=str(exc))

        logger.info("call_initiated", call_id=call_id, initiator=initiator_id, type=call_type)
        return call

    async def accept_call(self, call_id: str, user_id: str) -> ActiveCall:
        """Accept and join a call. Uses async lock to prevent race conditions."""
        async with self._lock:
            call = self._get_call(call_id)
            if user_id in self._user_calls:
                raise ValueError("User already in a call")
            if call.status == "ended":
                raise ValueError("Call has already ended")

            call.add_participant(user_id)
            call.status = "active"
            call.started_at = datetime.now(timezone.utc)
            self._user_calls[user_id] = call_id

        try:
            from app.services.call_state_persistence import call_state_persistence
            await call_state_persistence.add_participant(
                call_id=call_id, user_id=user_id, sid=None, role="participant",
            )
            await call_state_persistence.mark_active(call_id)
        except Exception as exc:
            logger.error("call_persist_accept_failed", call_id=call_id, error=str(exc))

        try:
            from app.services.distributed_group_call_state import get_group_call_state
            await get_group_call_state().add_participant(call_id, user_id)
        except Exception as exc:
            logger.warning("gcs_accept_mirror_failed", call_id=call_id, error=str(exc))

        logger.info("call_accepted", call_id=call_id, user_id=user_id)
        return call

    async def reject_call(self, call_id: str, user_id: str) -> ActiveCall:
        """Reject an incoming call."""
        async with self._lock:
            call = self._get_call(call_id)
            if call.status == "ended":
                return call  # Already ended, no-op
            # Only end if it was a 1-to-1 call with only the initiator
            if len(call.participants) <= 1:
                call.status = "ended"
                call.ended_at = datetime.now(timezone.utc)
                self._cleanup_call(call)
        logger.info("call_rejected", call_id=call_id, user_id=user_id)
        return call

    async def join_group_call(self, call_id: str, user_id: str) -> ActiveCall:
        """Join an existing group call. Uses async lock to prevent race conditions.

        DB persistence and topology re-evaluation are fire-and-forget so that
        the caller's ack doesn't block on SQLite's single-writer lock under
        highly concurrent joins.
        """
        async with self._lock:
            call = self._get_call(call_id)
            if user_id in self._user_calls:
                raise ValueError("User already in a call")
            if call.status == "ended":
                raise ValueError("Call has already ended")
            if len(call.participants) >= MAX_CALL_PARTICIPANTS:
                raise ValueError(f"Call is full (max {MAX_CALL_PARTICIPANTS} participants)")

            call.add_participant(user_id)
            # Only the first joiner that transitions ringing→active should
            # schedule the DB mark_active write. Otherwise every concurrent
            # joiner in a mass-join burst races to UPDATE the same row and
            # starves the SQLite writer lock.
            became_active = False
            if call.status == "ringing":
                call.status = "active"
                call.started_at = datetime.now(timezone.utc)
                became_active = True
            self._user_calls[user_id] = call_id

        self._schedule_join_persist(call_id, user_id, mark_active=became_active)
        self._schedule_topology_reevaluate(call)

        # Mirror to distributed_group_call_state. Best-effort.
        try:
            from app.services.distributed_group_call_state import get_group_call_state
            await get_group_call_state().add_participant(call_id, user_id)
        except Exception as exc:
            logger.warning("gcs_join_mirror_failed", call_id=call_id, error=str(exc))

        logger.info("call_joined", call_id=call_id, user_id=user_id)
        return call

    def _schedule_join_persist(self, call_id: str, user_id: str, mark_active: bool) -> None:
        """Fire-and-forget DB persistence for a join. Keeps hot path non-blocking.

        Uses a batched writer so mass-join bursts collapse to a handful of
        transactions instead of one writer-lock acquisition per participant.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        from app.services.call_participant_batcher import call_participant_batcher
        call_participant_batcher.enqueue_add(call_id, user_id)

        if mark_active:
            async def _mark_active() -> None:
                try:
                    from app.services.call_state_persistence import call_state_persistence
                    await call_state_persistence.mark_active(call_id)
                except Exception as exc:
                    logger.error("call_mark_active_failed", call_id=call_id, error=str(exc))

            task = loop.create_task(_mark_active())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    def _schedule_topology_reevaluate(self, call: ActiveCall) -> None:
        """Run topology auto-switch off the hot path."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _runner() -> None:
            try:
                from app.services.topology_manager import topology_manager
                await topology_manager.reevaluate(call)
            except Exception as exc:
                logger.debug("topology_reevaluate_skipped", error=str(exc))

        task = loop.create_task(_runner())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def leave_call(self, call_id: str, user_id: str) -> ActiveCall:
        """Leave a call. End call if no participants remain."""
        promoted_host: str | None = None
        async with self._lock:
            call = self._get_call(call_id)
            was_initiator = (call.initiator_id == user_id)
            call.remove_participant(user_id)
            self._user_calls.pop(user_id, None)

            if len(call.participants) == 0:
                call.status = "ended"
                call.ended_at = datetime.now(timezone.utc)
                self._cleanup_call(call)
            elif len(call.participants) == 1 and call.routing == "p2p":
                # P2P call: end when one participant leaves
                call.status = "ended"
                call.ended_at = datetime.now(timezone.utc)
                self._cleanup_call(call)
            elif was_initiator:
                # Host promotion — without this, the call is hostless:
                # nobody can end-for-everyone or kick a misbehaving
                # member, and `initiator_id` points to a user who's no
                # longer there. For group calls only — p2p is already
                # handled above.
                #
                # Selection priority (highest first):
                #   1. ChannelMember.role = "admin" (longest-joined wins ties)
                #   2. ChannelMember.role = "moderator"
                #   3. Longest-joined regular participant
                #
                # Without role-awareness, a user who shouldn't have
                # moderation power could become host purely by being
                # the first to join.
                if call.routing != "p2p" and call.participants:
                    promoted_host = await self._pick_promoted_host(
                        call, user_id,
                    )
                    if promoted_host:
                        call.initiator_id = promoted_host
                        call.append_event("call:host-changed", {
                            "call_id": call_id,
                            "old_host": user_id,
                            "new_host": promoted_host,
                        })
        # We can't emit from inside the lock (sio.emit awaits its own
        # locks). Emit after the section.
        if promoted_host:
            try:
                from app.socket.server import sio as _sio
                await _sio.emit("call:host-changed", {
                    "call_id": call_id,
                    "old_host": user_id,
                    "new_host": promoted_host,
                }, room=f"call:{call_id}")
                logger.info("call_host_promoted", call_id=call_id, new_host=promoted_host)
            except Exception as exc:
                logger.warning("host_promotion_emit_failed", call_id=call_id, error=str(exc))

        try:
            from app.services.call_participant_batcher import call_participant_batcher
            call_participant_batcher.enqueue_remove(call_id, user_id)
            if call.status == "ended":
                # Call-ended is a lifecycle write — durable path stays direct.
                from app.services.call_state_persistence import call_state_persistence
                await call_state_persistence.mark_ended(call_id, reason="leave")
        except Exception as exc:
            logger.error("call_persist_leave_failed", call_id=call_id, error=str(exc))

        # Mirror to distributed_group_call_state. We always remove the
        # leaver; if the whole call ended, also tear down the call-
        # level state so other servers' caches drop it immediately.
        try:
            from app.services.distributed_group_call_state import get_group_call_state
            gcs = get_group_call_state()
            await gcs.remove_participant(call_id, user_id)
            if call.status == "ended":
                await gcs.end_call(call_id)
        except Exception as exc:
            logger.warning("gcs_leave_mirror_failed", call_id=call_id, error=str(exc))

        # Topology may want to downgrade SFU→mesh now
        try:
            if call.status != "ended":
                from app.services.topology_manager import topology_manager
                await topology_manager.reevaluate(call)
        except Exception:
            pass

        logger.info("call_left", call_id=call_id, user_id=user_id, remaining=len(call.participants))
        return call

    async def hangup(self, call_id: str, user_id: str) -> ActiveCall:
        """Hang up — end the call for everyone."""
        async with self._lock:
            call = self._get_call(call_id)
            call.status = "ended"
            call.ended_at = datetime.now(timezone.utc)
            self._cleanup_call(call)
        try:
            from app.services.call_state_persistence import call_state_persistence
            await call_state_persistence.mark_ended(call_id, reason="hangup")
        except Exception as exc:
            logger.error("call_persist_hangup_failed", call_id=call_id, error=str(exc))
        try:
            from app.services.distributed_group_call_state import get_group_call_state
            await get_group_call_state().end_call(call_id)
        except Exception as exc:
            logger.warning("gcs_hangup_mirror_failed", call_id=call_id, error=str(exc))
        logger.info("call_hangup", call_id=call_id, user_id=user_id)
        return call

    # ── Restart / multi-worker recovery ─────────────────────────────────────

    async def reap_ended_calls(self, call_ids: list[str]) -> int:
        """
        Called by the DB-level orphan sweep after it marks calls as ``ended``
        in persistent storage. Brings in-memory state in sync: removes the
        :class:`ActiveCall`, releases SFU routers, and cleans presenter state.
        Idempotent — unknown call_ids are silently ignored.

        Returns the number of in-memory calls actually reaped.
        """
        if not call_ids:
            return 0
        reaped = 0
        async with self._lock:
            for cid in call_ids:
                call = self._active_calls.get(cid)
                if not call:
                    continue
                call.status = "ended"
                call.ended_at = datetime.now(timezone.utc)
                self._cleanup_call(call)   # fires topology_manager.on_call_ended
                reaped += 1
        if reaped:
            logger.warning("calls_reaped_from_db_sweep", count=reaped)
        return reaped

    async def rehydrate_from_db(self) -> int:
        """
        Called on server startup — rebuild in-memory cache from the
        ``active_calls`` table so a crash or deploy doesn't drop live calls.
        Returns the number of calls restored.
        """
        try:
            from app.services.call_state_persistence import call_state_persistence
            rows = await call_state_persistence.rehydrate_live_calls()
        except Exception as exc:
            logger.error("call_rehydrate_failed", error=str(exc))
            return 0

        restored = 0
        restored_generations: list[tuple[str, int, str]] = []
        async with self._lock:
            for r in rows:
                if r["call_id"] in self._active_calls:
                    continue
                call = ActiveCall(
                    call_id=r["call_id"],
                    initiator_id=r["initiator_id"],
                    call_type=r["call_type"],
                    routing=r["routing"],
                    channel_id=r["channel_id"],
                )
                call.status = r["status"]
                for p in r["participants"]:
                    call.add_participant(p["user_id"])
                    call.participants[p["user_id"]]["muted"] = p["muted"]
                    call.participants[p["user_id"]]["video_off"] = p["video_off"]
                    call.participants[p["user_id"]]["sharing_screen"] = p["sharing_screen"]
                    self._user_calls[p["user_id"]] = r["call_id"]
                    if p["on_hold"]:
                        call.on_hold_users.add(p["user_id"])
                if r["started_at"]:
                    from datetime import datetime as _dt
                    call.started_at = _dt.fromisoformat(r["started_at"])
                self._active_calls[r["call_id"]] = call
                restored += 1
                restored_generations.append(
                    (r["call_id"], int(r.get("topology_generation") or 1), r["routing"]),
                )

        # Seed TopologyManager so client acks line up with the persisted
        # generation counter. Also invalidate stale SFU router info — the
        # mediasoup worker lost its router on process death; next reevaluate
        # will either re-allocate or downgrade to mesh.
        if restored_generations:
            try:
                from app.services.topology_manager import topology_manager
                for call_id, gen, routing in restored_generations:
                    topology_manager.restore_generation(call_id, gen)
                    if routing == "sfu":
                        topology_manager.mark_router_stale(call_id)
            except Exception as exc:
                logger.error("topology_rehydrate_failed", error=str(exc))

        if restored:
            logger.info("calls_rehydrated", count=restored)
        return restored

    async def toggle_mute(self, user_id: str, muted: bool) -> ActiveCall | None:
        """Toggle mute state for a user in their current call."""
        async with self._lock:
            call_id = self._user_calls.get(user_id)
            if not call_id:
                return None
            call = self._active_calls.get(call_id)
            if call and user_id in call.participants:
                call.participants[user_id]["muted"] = muted
        if call_id and call:
            try:
                from app.services.call_state_persistence import call_state_persistence
                await call_state_persistence.update_participant_flags(
                    call_id, user_id, muted=muted,
                )
            except Exception:
                pass
            try:
                from app.services.distributed_group_call_state import get_group_call_state
                await get_group_call_state().update_flags(
                    call_id, user_id, is_muted=muted,
                )
            except Exception:
                pass
        return call

    async def toggle_video(self, user_id: str, video_off: bool) -> ActiveCall | None:
        """Toggle video state for a user in their current call."""
        async with self._lock:
            call_id = self._user_calls.get(user_id)
            if not call_id:
                return None
            call = self._active_calls.get(call_id)
            if call and user_id in call.participants:
                call.participants[user_id]["video_off"] = video_off
        if call_id and call:
            try:
                from app.services.call_state_persistence import call_state_persistence
                await call_state_persistence.update_participant_flags(
                    call_id, user_id, video_off=video_off,
                )
            except Exception:
                pass
            try:
                from app.services.distributed_group_call_state import get_group_call_state
                await get_group_call_state().update_flags(
                    call_id, user_id, is_video_off=video_off,
                )
            except Exception:
                pass
        return call

    async def toggle_screen_share(self, user_id: str, sharing: bool) -> ActiveCall | None:
        """Toggle screen share state for a user in their current call."""
        async with self._lock:
            call_id = self._user_calls.get(user_id)
            if not call_id:
                return None
            call = self._active_calls.get(call_id)
            if call and user_id in call.participants:
                call.participants[user_id]["sharing_screen"] = sharing
        if call_id and call:
            try:
                from app.services.call_state_persistence import call_state_persistence
                await call_state_persistence.update_participant_flags(
                    call_id, user_id, sharing_screen=sharing,
                )
            except Exception:
                pass
        return call

    # ── Breakout rooms ──────────────────────────────────────────
    #
    # The host can carve participants into named sub-groups. Each
    # group gets its own Socket.IO sub-room (``call:<id>:breakout:N``)
    # and the clients in that group rebuild their mesh among
    # themselves. The MAIN room stays alive — when breakouts close,
    # everyone re-joins the main mesh.
    #
    # Server state is purely advisory: we record the assignment so
    # late joiners + the post-call summary know who was where. The
    # actual signaling fan-out happens via the room emit that
    # ``v2_call_breakout_open`` triggers.

    def _ensure_breakout_fields(self, call: "ActiveCall") -> None:
        if not hasattr(call, "breakouts"):
            call.breakouts = []  # type: ignore[attr-defined]
        if not hasattr(call, "breakout_assignments"):
            call.breakout_assignments = {}  # type: ignore[attr-defined]

    async def open_breakouts(
        self, call_id: str, by_user_id: str,
        groups: list[dict],
    ) -> bool:
        """Open breakouts. ``groups`` is a list of:
        ``[{"id": "g1", "name": "Group A", "members": [user_ids]}, ...]``.
        Only the host may invoke. Idempotent — replaces any
        previous breakout configuration.
        """
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call or call.initiator_id != by_user_id:
                return False
            self._ensure_breakout_fields(call)
            # Validate: every member must be a real participant.
            valid_groups = []
            assignments: dict[str, str] = {}
            for g in groups:
                gid = str(g.get("id") or "").strip()
                name = str(g.get("name") or "").strip() or gid
                members = [
                    m for m in (g.get("members") or [])
                    if m in call.participants
                ]
                if not gid or not members:
                    continue
                valid_groups.append({"id": gid, "name": name, "members": members})
                for m in members:
                    assignments[m] = gid
            call.breakouts = valid_groups  # type: ignore[attr-defined]
            call.breakout_assignments = assignments  # type: ignore[attr-defined]
            return True

    async def close_breakouts(self, call_id: str, by_user_id: str) -> bool:
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call or call.initiator_id != by_user_id:
                return False
            self._ensure_breakout_fields(call)
            call.breakouts = []  # type: ignore[attr-defined]
            call.breakout_assignments = {}  # type: ignore[attr-defined]
            return True

    def get_breakouts(self, call_id: str) -> dict:
        call = self._active_calls.get(call_id)
        if not call:
            return {"groups": [], "assignments": {}}
        self._ensure_breakout_fields(call)
        return {
            "groups": list(call.breakouts),  # type: ignore[attr-defined]
            "assignments": dict(call.breakout_assignments),  # type: ignore[attr-defined]
        }

    # ── Per-call passcode (PIN gate) ────────────────────────────
    #
    # When ``call.passcode_hash`` is set, every joiner must present
    # the matching plain-text PIN. The PIN is hashed (PBKDF2-SHA256)
    # before storing so a casual DB peek doesn't leak it. Empty PIN
    # disables the gate. The host can rotate it any time.
    #
    # Storage is in-memory + lobby_pending fallback because PINs are
    # ephemeral per-call: when the call ends, the hash dies with it.

    @staticmethod
    def _hash_passcode(plain: str) -> str:
        """PBKDF2-SHA256 with a per-call random salt. Format:
        ``salt$iter$hex(digest)``."""
        import hashlib
        import os as _os
        import secrets as _secrets
        if not plain:
            return ""
        salt = _secrets.token_bytes(16)
        iters = 50_000  # ~25ms on a modest CPU — enough for PIN gates
        digest = hashlib.pbkdf2_hmac(
            "sha256", plain.encode("utf-8"), salt, iters,
        )
        return f"{salt.hex()}${iters}${digest.hex()}"

    @staticmethod
    def _verify_passcode(plain: str, stored: str) -> bool:
        if not stored:
            return True   # no gate
        try:
            import hashlib
            salt_hex, iters_str, want_hex = stored.split("$", 2)
            salt = bytes.fromhex(salt_hex)
            iters = int(iters_str)
            got = hashlib.pbkdf2_hmac(
                "sha256", (plain or "").encode("utf-8"), salt, iters,
            )
            return got.hex() == want_hex
        except Exception:
            return False

    async def set_call_passcode(
        self, call_id: str, plain: str, by_user_id: str,
    ) -> bool:
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call:
                return False
            # Only the host (initiator) can set the PIN.
            if call.initiator_id != by_user_id:
                return False
            call.passcode_hash = self._hash_passcode(plain)  # type: ignore[attr-defined]
            return True

    def has_passcode(self, call_id: str) -> bool:
        call = self._active_calls.get(call_id)
        return bool(getattr(call, "passcode_hash", None))

    def verify_passcode(self, call_id: str, plain: str) -> bool:
        call = self._active_calls.get(call_id)
        if not call:
            return False
        stored = getattr(call, "passcode_hash", "") or ""
        return self._verify_passcode(plain, stored)

    # ── Lobby / knock-to-enter ───────────────────────────────────
    #
    # When ``call.lobby_enabled`` is True, a join attempt by a non-
    # participant lands them in ``call.lobby_pending`` instead of
    # ``call.participants``. The host gets a Socket.IO event and
    # decides admit / deny. Admits move the user into participants
    # via the normal ``add_participant`` path.

    def _ensure_lobby_fields(self, call: "ActiveCall") -> None:
        """Lazily attach lobby attributes — keeps ActiveCall init lean
        when lobbies are off (the common case)."""
        if not hasattr(call, "lobby_enabled"):
            call.lobby_enabled = False  # type: ignore[attr-defined]
        if not hasattr(call, "lobby_pending"):
            call.lobby_pending = {}  # type: ignore[attr-defined]

    async def set_lobby_enabled(self, call_id: str, enabled: bool) -> bool:
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call:
                return False
            self._ensure_lobby_fields(call)
            call.lobby_enabled = bool(enabled)  # type: ignore[attr-defined]
            return True

    async def lobby_knock(
        self, call_id: str, user_id: str, display_name: str | None = None,
    ) -> str:
        """Add a user to the lobby. Returns 'queued' on success or
        'admitted_directly' when the lobby is off."""
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call:
                return "no_call"
            self._ensure_lobby_fields(call)
            if not call.lobby_enabled:  # type: ignore[attr-defined]
                return "admitted_directly"
            call.lobby_pending[user_id] = {  # type: ignore[attr-defined]
                "display_name": display_name or user_id,
                "knocked_at": datetime.now(timezone.utc).isoformat(),
            }
        return "queued"

    async def lobby_admit(self, call_id: str, user_id: str) -> bool:
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call:
                return False
            self._ensure_lobby_fields(call)
            entry = call.lobby_pending.pop(user_id, None)  # type: ignore[attr-defined]
            return entry is not None

    async def lobby_deny(self, call_id: str, user_id: str) -> bool:
        async with self._lock:
            call = self._active_calls.get(call_id)
            if not call:
                return False
            self._ensure_lobby_fields(call)
            return call.lobby_pending.pop(user_id, None) is not None  # type: ignore[attr-defined]

    def lobby_pending(self, call_id: str) -> dict[str, dict]:
        call = self._active_calls.get(call_id)
        if not call or not hasattr(call, "lobby_pending"):
            return {}
        return dict(call.lobby_pending)  # type: ignore[attr-defined]

    async def toggle_hand(self, user_id: str, raised: bool) -> ActiveCall | None:
        """Toggle raise-hand state for a user in their current call.

        Webinar/large-meeting feature: an audience member raises their
        hand to ask a question, the host sees the indicator and can
        unmute or spotlight them. Stored per-participant; the timestamp
        is included so the UI can sort hands in the order they were
        raised (FIFO queue is the standard convention).
        """
        async with self._lock:
            call_id = self._user_calls.get(user_id)
            if not call_id:
                return None
            call = self._active_calls.get(call_id)
            if call and user_id in call.participants:
                call.participants[user_id]["hand_raised"] = raised
                call.participants[user_id]["hand_raised_at"] = (
                    datetime.now(timezone.utc).isoformat() if raised else None
                )
                call.append_event("call:hand-changed", {
                    "call_id": call_id,
                    "user_id": user_id,
                    "raised": raised,
                })
        return call

    def get_user_call(self, user_id: str) -> ActiveCall | None:
        call_id = self._user_calls.get(user_id)
        return self._active_calls.get(call_id) if call_id else None

    def get_call(self, call_id: str) -> ActiveCall | None:
        return self._active_calls.get(call_id)

    def get_call_by_channel(self, channel_id: str) -> ActiveCall | None:
        """Find active call for a channel (group calls)."""
        for call in self._active_calls.values():
            if call.channel_id == channel_id and call.status != "ended":
                return call
        return None

    def _get_call(self, call_id: str) -> ActiveCall:
        call = self._active_calls.get(call_id)
        if not call:
            raise ValueError(f"Call {call_id} not found")
        return call

    async def _pick_promoted_host(
        self, call: ActiveCall, departing_user_id: str
    ) -> str | None:
        """Choose the next host when the current one leaves.

        Priority:
          1. ChannelMember.role == "admin"     — longest-joined wins ties
          2. ChannelMember.role == "moderator" — longest-joined wins ties
          3. Longest-joined regular participant
          4. None if no participants remain

        For DM (no channel_id) or any DB lookup error, falls back to the
        plain longest-joined heuristic — keeps the legacy behavior safe
        even on misconfigured deployments.
        """
        candidates = [
            (uid, p.get("joined_at") or datetime.now(timezone.utc))
            for uid, p in call.participants.items()
            if uid != departing_user_id
        ]
        if not candidates:
            return None

        def _by_joined_at(pair):
            return pair[1]

        # Plain longest-joined as fallback if role lookup is unavailable.
        legacy_pick = min(candidates, key=_by_joined_at)[0]

        if not getattr(call, "channel_id", None):
            return legacy_pick

        try:
            from sqlalchemy import select as _sel
            from app.models.channel import ChannelMember as _CM
            from app.db.session import async_session_factory as _sf
            user_ids = [uid for uid, _ in candidates]
            async with _sf() as db:
                rows = (await db.execute(
                    _sel(_CM.user_id, _CM.role).where(
                        _CM.channel_id == call.channel_id,
                        _CM.user_id.in_(user_ids),
                    )
                )).all()
            role_map = {uid: (role or "member") for uid, role in rows}
        except Exception as e:
            logger.debug("host_promotion_role_lookup_failed", error=str(e))
            return legacy_pick

        # Bucket by role + sort by joined_at within each bucket.
        admins = sorted(
            [(uid, ts) for uid, ts in candidates if role_map.get(uid) == "admin"],
            key=_by_joined_at,
        )
        moderators = sorted(
            [(uid, ts) for uid, ts in candidates if role_map.get(uid) == "moderator"],
            key=_by_joined_at,
        )
        if admins:
            return admins[0][0]
        if moderators:
            return moderators[0][0]
        return legacy_pick

    def _cleanup_call(self, call: ActiveCall) -> None:
        """Remove call from active tracking + cleanup presenter state + release topology.

        Every call-end path (leave_call when empty, hangup, reject_call, orphan
        cleanup loop) routes through here — so this is the single choke point
        where the SFU router must be released. ``topology_manager.on_call_ended``
        is fired as a tracked background task: we are running inside
        ``self._lock`` on most call sites and ``release_router`` may do HTTP
        I/O to the mediasoup control plane.
        """
        for uid in list(call.participants.keys()):
            self._user_calls.pop(uid, None)
        self._active_calls.pop(call.call_id, None)

        # Cleanup presenter state
        try:
            from app.services.presenter_service import presenter_service
            presenter_service.cleanup_call(call.call_id)
        except Exception as e:
            logger.error("presenter_cleanup_error", call_id=call.call_id, error=str(e))

        # Release SFU router + topology generation state (fire-and-forget)
        self._schedule_topology_release(call.call_id)

    def _schedule_topology_release(self, call_id: str) -> None:
        """Fire ``topology_manager.on_call_ended`` off-lock without losing the task."""
        try:
            from app.services.topology_manager import topology_manager
        except Exception as exc:  # pragma: no cover - import should always succeed
            logger.error("topology_import_failed", call_id=call_id, error=str(exc))
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called outside a running event loop (rare: sync teardown). Skip
            # — the process is likely dying and the worker will reap routers.
            return

        async def _runner() -> None:
            try:
                await topology_manager.on_call_ended(call_id)
            except Exception as exc:
                logger.error("topology_release_error", call_id=call_id, error=str(exc))

        task = loop.create_task(_runner())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def persist_call_log(self, db: AsyncSession, call: ActiveCall) -> CallLog | None:
        """Save completed call to database. Uses _max_participants for accurate count.

        Idempotent: returns ``None`` after the first successful write so the
        per-participant disconnect cleanup fan-in doesn't duplicate the row or
        thrash the SQLite writer lock on 1000-user calls.
        """
        if call._log_persisted:
            return None
        call._log_persisted = True  # claim eagerly — losers no-op
        try:
            duration = None
            if call.started_at and call.ended_at:
                duration = int((call.ended_at - call.started_at).total_seconds())

            # Use _max_participants (tracked during call) instead of len(participants) which may be empty
            participant_count = max(call._max_participants, 1)

            log = CallLog(
                channel_id=call.channel_id,
                initiator_id=call.initiator_id,
                call_type=call.call_type,
                routing=call.routing,
                status="ended",
                started_at=call.started_at,
                ended_at=call.ended_at,
                duration_seconds=duration,
                end_reason="hangup",
                participant_count=participant_count,
            )
            db.add(log)
            await db.commit()
            await db.refresh(log)
            logger.info("call_log_persisted", call_id=call.call_id, participant_count=participant_count, duration=duration)
            return log
        except Exception as e:
            logger.error("persist_call_log_failed", call_id=call.call_id, error=str(e))
            raise

    # ── Call Hold/Resume ───────────────────────────────────────

    async def hold_call(self, call_id: str, user_id: str) -> dict:
        """Place a call on hold for the specified user."""
        async with self._lock:
            call = self._get_call(call_id)
            if user_id not in call.participants:
                raise ValueError(f"User {user_id} not in call {call_id}")
            call.on_hold_users.add(user_id)
        logger.info("call_held", call_id=call_id, user_id=user_id)
        return {
            "call_id": call_id,
            "user_id": user_id,
            "on_hold": True,
        }

    async def resume_call(self, call_id: str, user_id: str) -> dict:
        """Resume a held call for the specified user."""
        async with self._lock:
            call = self._get_call(call_id)
            if user_id not in call.participants:
                raise ValueError(f"User {user_id} not in call {call_id}")
            call.on_hold_users.discard(user_id)
        logger.info("call_resumed", call_id=call_id, user_id=user_id)
        return {
            "call_id": call_id,
            "user_id": user_id,
            "on_hold": False,
        }

    # ── Call Quality Tracking ──────────────────────────────────

    async def report_quality(self, call_id: str, user_id: str, metrics: dict) -> None:
        """Record quality metrics for a user in a call. Keeps only last 100 reports per user."""
        async with self._lock:
            call = self._get_call(call_id)
            if user_id not in call.participants:
                raise ValueError(f"User {user_id} not in call {call_id}")

            if user_id not in call.quality_reports:
                call.quality_reports[user_id] = []

            report = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metrics": metrics,
            }
            call.quality_reports[user_id].append(report)

            # Trim to keep only last 100 reports per user (prevent unbounded growth)
            if len(call.quality_reports[user_id]) > 100:
                call.quality_reports[user_id] = call.quality_reports[user_id][-100:]

        logger.debug("quality_reported", call_id=call_id, user_id=user_id, metrics=metrics)

    async def get_call_quality(self, call_id: str) -> dict:
        """Retrieve quality metrics for all participants in a call."""
        call = self._get_call(call_id)
        return {
            "call_id": call_id,
            "quality_reports": call.quality_reports,
        }

    # ── Call Transfer ──────────────────────────────────────────

    async def transfer_call(self, call_id: str, from_user: str, to_user: str) -> dict:
        """Transfer a call from one user to another."""
        async with self._lock:
            call = self._get_call(call_id)
            if from_user not in call.participants:
                raise ValueError(f"User {from_user} not in call {call_id}")
            if to_user in call.participants:
                raise ValueError(f"User {to_user} already in call {call_id}")

            # Add transferee and remove original participant
            call.add_participant(to_user)
            call.remove_participant(from_user)
            self._user_calls.pop(from_user, None)
            self._user_calls[to_user] = call_id

        logger.info("call_transferred", call_id=call_id, from_user=from_user, to_user=to_user)
        return {
            "call_id": call_id,
            "from_user": from_user,
            "to_user": to_user,
            "status": "transferred",
        }

    # ── Rate Limiting ──────────────────────────────────────────

    def _check_rate_limit(self, user_id: str, limit: int = 5, window: float = 60.0) -> bool:
        """
        Check if user has exceeded call initiation rate limit.
        Uses a bounded deque(maxlen=20) to prevent unbounded growth.
        limit: max number of calls in time window
        window: time window in seconds
        Returns True if within limit, False if exceeded.
        """
        import time
        now = time.time()

        if user_id not in self._initiate_timestamps:
            self._initiate_timestamps[user_id] = deque(maxlen=20)

        # Remove timestamps outside the window
        while self._initiate_timestamps[user_id] and (now - self._initiate_timestamps[user_id][0]) >= window:
            self._initiate_timestamps[user_id].popleft()

        # Check if exceeded limit
        if len(self._initiate_timestamps[user_id]) >= limit:
            return False

        # Record this initiation (deque automatically drops oldest if at maxlen)
        self._initiate_timestamps[user_id].append(now)
        return True

    # ── Enhanced Call Stats ────────────────────────────────────

    async def get_active_calls_stats(self) -> dict:
        """Get aggregated statistics about all active calls."""
        total_call_count = len(self._active_calls)
        total_participant_count = 0
        active_calls = []
        total_duration_seconds = 0
        call_count_active = 0

        now = datetime.now(timezone.utc)

        for call in self._active_calls.values():
            if call.status == "active":
                call_count_active += 1
                total_participant_count += len(call.participants)
                if call.started_at:
                    duration = int((now - call.started_at).total_seconds())
                    total_duration_seconds += duration
                active_calls.append({
                    "call_id": call.call_id,
                    "participant_count": len(call.participants),
                    "duration_seconds": int((now - call.started_at).total_seconds()) if call.started_at else 0,
                    "call_type": call.call_type,
                    "routing": call.routing,
                })

        avg_duration = (
            total_duration_seconds / call_count_active if call_count_active > 0 else 0
        )

        return {
            "total_call_count": total_call_count,
            "active_call_count": call_count_active,
            "total_participant_count": total_participant_count,
            "avg_duration_seconds": avg_duration,
            "active_calls": active_calls,
            "timestamp": now.isoformat(),
        }

    async def get_call_details(self, call_id: str) -> dict:
        """Get detailed information about a specific call."""
        call = self._get_call(call_id)
        now = datetime.now(timezone.utc)

        duration = 0
        if call.started_at:
            if call.ended_at:
                duration = int((call.ended_at - call.started_at).total_seconds())
            else:
                duration = int((now - call.started_at).total_seconds())

        return {
            "call_id": call.call_id,
            "initiator_id": call.initiator_id,
            "call_type": call.call_type,
            "routing": call.routing,
            "channel_id": call.channel_id,
            "status": call.status,
            "duration_seconds": duration,
            "participant_count": len(call.participants),
            "participants": [
                {
                    "user_id": uid,
                    "joined_at": p["joined_at"].isoformat(),
                    "muted": p["muted"],
                    "video_off": p["video_off"],
                    "sharing_screen": p["sharing_screen"],
                    "on_hold": uid in call.on_hold_users,
                }
                for uid, p in call.participants.items()
            ],
            "quality_reports": call.quality_reports,
            "created_at": call.created_at.isoformat(),
            "started_at": call.started_at.isoformat() if call.started_at else None,
            "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        }


# Singleton instance — shared across the application
call_service = CallService()
