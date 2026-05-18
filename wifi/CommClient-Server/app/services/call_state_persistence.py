"""
Dual-layer call state — in-memory for hot reads + DB for truth/recovery.

The existing :class:`app.services.call_service.CallService` stays intact as a
fast cache. This service replicates every state change to the ``active_calls`` /
``active_call_participants`` / ``call_signal_events`` tables so that:

  * a worker crash + restart rehydrates live calls from disk
  * multi-worker uvicorn setups agree on a single source of truth
  * orphan detection can span processes
  * a reconnecting client can ``signal_replay`` the last N signals to recover
    its ICE state without dropping the call

All writes here are safe to fire-and-forget inside the event loop — failures
are logged but never mask a signaling operation (availability > durability
for *signaling*). Durability is enforced for lifecycle writes (initiate,
end) via :func:`ensure_durable_write`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.db.sqlite_tuning import ensure_durable_write
from app.models.active_call import (
    ActiveCall,
    ActiveCallParticipant,
    CallSignalEvent,
)

logger = get_logger(__name__)


SIGNAL_REPLAY_LIMIT = 256          # max events returned by signal_replay
SIGNAL_RETENTION_PER_CALL = 1024   # trim older rows beyond this per call
HEARTBEAT_STALE_SECONDS = 90       # mark calls orphan if no heartbeat this long


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _session() -> AsyncSession:
    """New async session — caller must close."""
    return async_session_factory()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Public API — mirrors CallService lifecycle events
# ─────────────────────────────────────────────────────────────────────────────

class CallStatePersistence:
    """Stateless — operates on the DB directly. Singleton for convenience."""

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def upsert_call(
        self,
        *,
        call_id: str,
        initiator_id: str,
        call_type: str,
        routing: str,
        channel_id: str | None,
        status: str = "ringing",
        max_participants: int = 1,
        topology_generation: int = 1,
        metadata: dict | None = None,
        origin_server_id: str | None = None,
    ) -> None:
        """Insert a new active_call row or upsert on restart.

        ``origin_server_id`` is populated automatically from
        ``discovery_service.get_server_id()`` if not supplied — the
        creating server is, by definition, the origin. Passing an
        explicit value is reserved for federation rehydrate paths
        where we need to preserve the origin from a different server.
        """
        if not origin_server_id:
            try:
                from app.services.discovery_service import get_server_id as _gs
                origin_server_id = _gs()
            except Exception:
                origin_server_id = None
        async with async_session_factory() as s:
            existing = await s.get(ActiveCall, call_id)
            now = _utc_now()
            if existing is None:
                row = ActiveCall(
                    id=call_id,
                    channel_id=channel_id,
                    initiator_id=initiator_id,
                    call_type=call_type,
                    routing=routing,
                    status=status,
                    max_participants=max_participants,
                    topology_generation=topology_generation,
                    last_heartbeat_at=now,
                    metadata_json=json.dumps(metadata) if metadata else None,
                    origin_server_id=origin_server_id,
                )
                s.add(row)
            else:
                existing.status = status
                existing.routing = routing
                existing.topology_generation = topology_generation
                existing.last_heartbeat_at = now
                if metadata is not None:
                    existing.metadata_json = json.dumps(metadata)
                # Origin only set on first INSERT — subsequent upserts
                # MUST NOT clobber it; it's the source of truth for
                # federation routing.
                if existing.origin_server_id is None and origin_server_id:
                    existing.origin_server_id = origin_server_id
            await ensure_durable_write(s)
            await s.commit()

    async def mark_active(self, call_id: str) -> None:
        async with async_session_factory() as s:
            await s.execute(
                update(ActiveCall)
                .where(ActiveCall.id == call_id)
                .values(status="active", started_at=_utc_now(), last_heartbeat_at=_utc_now())
            )
            await s.commit()

    async def mark_ended(self, call_id: str, reason: str | None = None) -> None:
        async with async_session_factory() as s:
            now = _utc_now()
            values: dict[str, Any] = {
                "status": "ended",
                "ended_at": now,
                "last_heartbeat_at": now,
            }
            if reason:
                values["metadata_json"] = json.dumps({"end_reason": reason})
            await s.execute(
                update(ActiveCall).where(ActiveCall.id == call_id).values(**values)
            )
            await s.execute(
                update(ActiveCallParticipant)
                .where(
                    ActiveCallParticipant.call_id == call_id,
                    ActiveCallParticipant.left_at.is_(None),
                )
                .values(left_at=now)
            )
            await ensure_durable_write(s)
            await s.commit()

    async def heartbeat(self, call_id: str) -> None:
        """Cheap keep-alive — called from CallService state mutations
        AND from the v2 client's call_heartbeat socket event.

        BLOCKER-4 fix: previously a plain UPDATE that silently did
        nothing if the row hadn't been INSERTed yet (race between
        initiate_call's DB write and the first heartbeat). Now an
        UPDATE-then-stats-check + bounded retry that tolerates the
        race window while still surfacing genuine state-loss bugs.

        Retry behaviour:
          * Initial UPDATE.
          * If rowcount=0 AND the call is still live in memory (i.e.
            this is genuinely a write-vs-write race, not a stale
            ghost), retry up to 3 times with a 25/50/100 ms backoff.
          * If still missing after retries, attempt a single
            best-effort hydrate from the in-memory ``ActiveCall`` so
            the next heartbeat lands on a real row.
          * If the call isn't live in memory, log warning once and
            give up — heartbeat on a non-existent call is benign.

        We deliberately don't auto-INSERT a blank row — that would
        mask state-loss bugs and produce ghost calls.
        """
        delays = (0.025, 0.05, 0.10)  # 25 ms → 50 ms → 100 ms
        for attempt, delay in enumerate((0.0,) + delays):
            if delay > 0:
                await asyncio.sleep(delay)
            async with async_session_factory() as s:
                res = await s.execute(
                    update(ActiveCall)
                    .where(ActiveCall.id == call_id)
                    .values(last_heartbeat_at=_utc_now())
                )
                await s.commit()
                if getattr(res, "rowcount", 0) > 0:
                    return
            # rowcount=0 — check if this is a transient race or a
            # truly-dead call before retrying.
            try:
                from app.services.call_service import call_service
                if call_id not in call_service._active_calls:  # type: ignore[attr-defined]
                    # Not live in memory either — give up with a single
                    # observable warning and don't retry forever.
                    if attempt == 0:
                        logger.warning(
                            "heartbeat_call_not_found",
                            call_id=call_id,
                            note="row missing AND not in memory — already ended or never started",
                        )
                    return
            except Exception:  # pragma: no cover — defensive
                pass

        # All retries exhausted; the call IS live in memory but the
        # row isn't there. Upsert from memory so the next heartbeat
        # lands on a real row.
        try:
            from app.services.call_service import call_service
            live = call_service._active_calls.get(call_id)  # type: ignore[attr-defined]
            if live is not None:
                await self.upsert_call(
                    call_id=call_id,
                    initiator_id=live.initiator_id,
                    call_type=getattr(live, "call_type", "audio"),
                    routing=getattr(live, "routing", "p2p"),
                    channel_id=getattr(live, "channel_id", None),
                    status=getattr(live, "status", "active"),
                    max_participants=len(getattr(live, "participants", {})) or 1,
                    origin_server_id=getattr(live, "origin_server_id", None),
                )
                logger.info(
                    "heartbeat_rehydrated_row_from_memory",
                    call_id=call_id,
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "heartbeat_rehydrate_failed",
                call_id=call_id, error=str(exc),
            )

    async def bump_topology(self, call_id: str, new_routing: str, generation: int) -> None:
        async with async_session_factory() as s:
            await s.execute(
                update(ActiveCall)
                .where(ActiveCall.id == call_id)
                .values(
                    routing=new_routing,
                    topology_generation=generation,
                    last_heartbeat_at=_utc_now(),
                )
            )
            await ensure_durable_write(s)
            await s.commit()

    # ── Participants ────────────────────────────────────────────────────────

    async def add_participant(
        self,
        *,
        call_id: str,
        user_id: str,
        sid: str | None,
        role: str = "participant",
    ) -> None:
        # Hot path under large group joins — keep this cheap:
        #   • No ensure_durable_write fsync (WAL + synchronous=NORMAL is crash-safe
        #     for non-critical participant rows; the call_log is the durable record).
        #   • No COUNT scan for max_participants — CallService tracks live size in
        #     memory and updates the column at end-of-call via persist_call_log.
        async with async_session_factory() as s:
            row = await s.scalar(
                select(ActiveCallParticipant).where(
                    ActiveCallParticipant.call_id == call_id,
                    ActiveCallParticipant.user_id == user_id,
                )
            )
            now = _utc_now()
            if row is None:
                s.add(ActiveCallParticipant(
                    call_id=call_id,
                    user_id=user_id,
                    sid=sid,
                    role=role,
                    joined_at=now,
                ))
            else:
                row.sid = sid
                row.role = role
                row.left_at = None
                row.joined_at = now
            await s.commit()

    async def remove_participant(self, call_id: str, user_id: str) -> None:
        async with async_session_factory() as s:
            await s.execute(
                update(ActiveCallParticipant)
                .where(
                    ActiveCallParticipant.call_id == call_id,
                    ActiveCallParticipant.user_id == user_id,
                    ActiveCallParticipant.left_at.is_(None),
                )
                .values(left_at=_utc_now())
            )
            await s.execute(
                update(ActiveCall)
                .where(ActiveCall.id == call_id)
                .values(last_heartbeat_at=_utc_now())
            )
            await s.commit()

    async def update_participant_flags(
        self,
        call_id: str,
        user_id: str,
        *,
        muted: bool | None = None,
        video_off: bool | None = None,
        sharing_screen: bool | None = None,
        on_hold: bool | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if muted is not None:
            values["muted"] = muted
        if video_off is not None:
            values["video_off"] = video_off
        if sharing_screen is not None:
            values["sharing_screen"] = sharing_screen
        if on_hold is not None:
            values["on_hold"] = on_hold
        if not values:
            return
        async with async_session_factory() as s:
            await s.execute(
                update(ActiveCallParticipant)
                .where(
                    ActiveCallParticipant.call_id == call_id,
                    ActiveCallParticipant.user_id == user_id,
                )
                .values(**values)
            )
            await s.commit()

    async def record_quality(
        self, call_id: str, user_id: str, metrics: dict
    ) -> None:
        async with async_session_factory() as s:
            await s.execute(
                update(ActiveCallParticipant)
                .where(
                    ActiveCallParticipant.call_id == call_id,
                    ActiveCallParticipant.user_id == user_id,
                )
                .values(
                    last_quality_json=json.dumps(metrics),
                    last_quality_at=_utc_now(),
                )
            )
            await s.commit()

    # ── Signaling replay log ────────────────────────────────────────────────

    async def append_signal(
        self,
        *,
        call_id: str,
        from_user: str,
        to_user: str | None,
        kind: str,
        payload: Any,
        topology_generation: int,
    ) -> None:
        async with async_session_factory() as s:
            s.add(CallSignalEvent(
                call_id=call_id,
                from_user=from_user,
                to_user=to_user,
                kind=kind,
                payload=json.dumps(payload) if not isinstance(payload, str) else payload,
                topology_generation=topology_generation,
            ))
            await s.commit()

    async def replay_signals(
        self,
        call_id: str,
        *,
        for_user: str | None = None,
        since_generation: int | None = None,
        limit: int = SIGNAL_REPLAY_LIMIT,
    ) -> tuple[list[dict], bool]:
        """
        Returns ``(events, truncated)``.

        ``truncated=True`` means the replay window had more matching signals
        than the ``SIGNAL_REPLAY_LIMIT`` (256 by default). In that case the
        client should NOT trust replay alone — it should request a full
        renegotiate (recreate every peer connection) instead of trying to
        patch ICE state from partial history, because any missing offer/answer
        pair would leave the mesh in an inconsistent state.
        """
        effective_limit = min(limit, SIGNAL_REPLAY_LIMIT)
        async with async_session_factory() as s:
            # Count first so we can signal truncation honestly.
            from sqlalchemy import func, or_
            count_q = (
                select(func.count(CallSignalEvent.id))
                .where(CallSignalEvent.call_id == call_id)
            )
            if for_user is not None:
                count_q = count_q.where(or_(
                    CallSignalEvent.to_user.is_(None),
                    CallSignalEvent.to_user == for_user,
                ))
            if since_generation is not None:
                count_q = count_q.where(
                    CallSignalEvent.topology_generation >= since_generation
                )
            total_matching = int((await s.scalar(count_q)) or 0)

            q = (
                select(CallSignalEvent)
                .where(CallSignalEvent.call_id == call_id)
                .order_by(CallSignalEvent.created_at.desc())
                .limit(effective_limit)
            )
            if for_user is not None:
                q = q.where(or_(
                    CallSignalEvent.to_user.is_(None),
                    CallSignalEvent.to_user == for_user,
                ))
            if since_generation is not None:
                q = q.where(CallSignalEvent.topology_generation >= since_generation)
            rows = (await s.scalars(q)).all()

            events = [
                {
                    "id": r.id,
                    "from": r.from_user,
                    "to": r.to_user,
                    "kind": r.kind,
                    "payload": json.loads(r.payload) if r.payload else None,
                    "topology_generation": r.topology_generation,
                    "at": r.created_at.isoformat(),
                }
                for r in reversed(rows)  # oldest first for replay
            ]
            truncated = total_matching > effective_limit
            return events, truncated

    async def trim_signals(self, call_id: str, keep: int = SIGNAL_RETENTION_PER_CALL) -> None:
        async with async_session_factory() as s:
            subq = (
                select(CallSignalEvent.id)
                .where(CallSignalEvent.call_id == call_id)
                .order_by(CallSignalEvent.created_at.desc())
                .offset(keep)
            )
            ids = [r for r in (await s.scalars(subq)).all()]
            if ids:
                await s.execute(
                    delete(CallSignalEvent).where(CallSignalEvent.id.in_(ids))
                )
                await s.commit()

    # ── Recovery / multi-worker ────────────────────────────────────────────

    async def rehydrate_live_calls(self) -> list[dict]:
        """
        Called at server startup. Returns a serializable list of active calls
        so :class:`CallService` can repopulate its in-memory cache.
        """
        async with async_session_factory() as s:
            rows = (await s.scalars(
                select(ActiveCall).where(ActiveCall.status.in_(["ringing", "active"]))
            )).all()
            result: list[dict] = []
            for r in rows:
                parts = (await s.scalars(
                    select(ActiveCallParticipant).where(
                        ActiveCallParticipant.call_id == r.id,
                        ActiveCallParticipant.left_at.is_(None),
                    )
                )).all()
                result.append({
                    "call_id": r.id,
                    "initiator_id": r.initiator_id,
                    "call_type": r.call_type,
                    "routing": r.routing,
                    "channel_id": r.channel_id,
                    "status": r.status,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "topology_generation": r.topology_generation,
                    "max_participants": r.max_participants,
                    "participants": [
                        {
                            "user_id": p.user_id,
                            "sid": p.sid,
                            "role": p.role,
                            "muted": p.muted,
                            "video_off": p.video_off,
                            "sharing_screen": p.sharing_screen,
                            "on_hold": p.on_hold,
                            "joined_at": p.joined_at.isoformat(),
                        }
                        for p in parts
                    ],
                })
            return result

    async def sweep_orphans(self) -> list[str]:
        """Mark calls as ended if no heartbeat in HEARTBEAT_STALE_SECONDS.

        Returns the list of ``call_id`` values that were marked ended so the
        caller can propagate the state change to in-memory caches
        (``CallService._active_calls``, ``TopologyManager._router_info``,
        presenter state, etc.). Returning just a count like the previous
        implementation created a split-brain: the DB said the call was over,
        but in-memory state still saw it as live, leaking SFU routers and
        letting stale clients continue pushing signaling messages.
        """
        cutoff = _utc_now() - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
        swept_ids: list[str] = []
        async with async_session_factory() as s:
            rows = (await s.scalars(
                select(ActiveCall).where(
                    ActiveCall.status.in_(["ringing", "active"]),
                    ActiveCall.last_heartbeat_at < cutoff,
                )
            )).all()
            for r in rows:
                r.status = "ended"
                r.ended_at = _utc_now()
                r.metadata_json = json.dumps({"end_reason": "heartbeat_timeout"})
                await s.execute(
                    update(ActiveCallParticipant)
                    .where(
                        ActiveCallParticipant.call_id == r.id,
                        ActiveCallParticipant.left_at.is_(None),
                    )
                    .values(left_at=_utc_now())
                )
                swept_ids.append(r.id)
            if rows:
                await ensure_durable_write(s)
                await s.commit()
                logger.warning("orphan_calls_swept", count=len(rows))
            return swept_ids

    # ── Cross-server discovery (Join Existing Call UX) ──────────────────────

    async def get_active_by_channel(self, channel_id: str) -> dict | None:
        """Return the active group call for a channel from DB, or None.

        Used by :func:`app.api.routes.calls.get_channel_active_call` as a
        fallback when the call is hosted on a sibling Helen server. The DB
        is replicated/shared across the cluster (or each server writes its
        own copy and federation reconciles via cluster_mesh) so every
        member of the channel can discover the call regardless of where
        it was started.

        Mirrors the in-memory ActiveCall payload shape returned by the
        REST endpoint to keep the client unchanged across both paths.
        """
        async with async_session_factory() as s:
            r = (await s.scalars(
                select(ActiveCall).where(
                    ActiveCall.channel_id == channel_id,
                    ActiveCall.status.in_(["ringing", "active"]),
                ).order_by(ActiveCall.created_at.desc()).limit(1)
            )).first()
            if not r:
                return None

            parts = (await s.scalars(
                select(ActiveCallParticipant).where(
                    ActiveCallParticipant.call_id == r.id,
                    ActiveCallParticipant.left_at.is_(None),
                )
            )).all()

            return {
                "call_id": r.id,
                "call_type": r.call_type,
                "routing": r.routing,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "participant_count": len(parts),
                "participants": [
                    {
                        "user_id": p.user_id,
                        "muted": bool(p.muted),
                        "video_off": bool(p.video_off),
                        "sharing_screen": bool(p.sharing_screen),
                        "on_hold": bool(p.on_hold),
                    }
                    for p in parts
                ],
                "host_id": r.initiator_id,
                "origin_server_id": r.origin_server_id,
            }

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _count_live_participants_session(
        self, s: AsyncSession, call_id: str
    ) -> int:
        from sqlalchemy import func
        count = await s.scalar(
            select(func.count())
            .select_from(ActiveCallParticipant)
            .where(
                ActiveCallParticipant.call_id == call_id,
                ActiveCallParticipant.left_at.is_(None),
            )
        )
        return int(count or 0)

    # ── Origin election support (Phase 1 distributed transformation) ──

    async def list_owned_by(self, server_id: str) -> list[str]:
        """Return call_ids whose ``origin_server_id`` is ``server_id``
        AND status is not "ended". Used by ``OriginElectionService``
        sweeper to find calls abandoned by a dead server so they can
        be re-elected.

        Note: ``ActiveCall.id`` IS the call_id — the model uses the
        call_id as primary key (see active_call.py:50–51).
        """
        async with async_session_factory() as s:
            try:
                result = await s.execute(
                    select(ActiveCall.id).where(
                        ActiveCall.origin_server_id == server_id,
                        ActiveCall.status != "ended",
                    )
                )
                return [r[0] for r in result.all()]
            except Exception as e:
                logger.warning(
                    "list_owned_by_failed",
                    server_id=server_id, error=str(e),
                )
                return []

    async def update_origin(self, call_id: str, new_origin_server_id: str) -> bool:
        """Migrate ``call_id`` to a new origin server. Called by
        ``OriginElectionService`` after a successful re-election.
        Returns True if a row was updated."""
        async with async_session_factory() as s:
            try:
                result = await s.execute(
                    update(ActiveCall)
                    .where(ActiveCall.id == call_id)
                    .values(
                        origin_server_id=new_origin_server_id,
                        last_heartbeat_at=_utc_now(),
                    )
                )
                await s.commit()
                ok = (result.rowcount or 0) > 0
                if ok:
                    logger.info(
                        "call_origin_migrated",
                        call_id=call_id,
                        new_origin=new_origin_server_id,
                    )
                return ok
            except Exception as e:
                logger.warning(
                    "update_origin_failed",
                    call_id=call_id,
                    new_origin=new_origin_server_id,
                    error=str(e),
                )
                return False


# Singleton
call_state_persistence = CallStatePersistence()
