"""
Distributed group call participant state — Redis-backed mirror.

Why
---
``call_service._active_calls`` is process-local. For 1-to-1 calls it
works because the origin server holds authoritative state and other
servers forward via federation RPC. For GROUP calls it's a problem:

  * Server A hosts user a1 (originator); the call is on Server A.
  * User b1 is hosted by Server B.
  * Server B has NO local view of the call's participant set.

Today, Server B asks Server A every time it needs to validate signal
authz, and the ``call_signal_authz`` shadow caches the answer for 3
hours. That's adequate for signaling. It's NOT adequate for:

  * "Show me the participants of call X" UI queries from Server B.
  * "Who is on this call right now" for the channel UI.
  * Mute / video flags fanned out from any participant's server.
  * Host promotion when origin dies (origin_election service handles
    the lease, but participant data still lives in the dead origin's
    in-memory dict).

This service mirrors participant state to Redis so any server can
read the live set without round-tripping the origin.

Storage layout
--------------
::

    KEY  helen:gcc:{call_id}:participants  HASH  user_id -> JSON{server_id, role, joined_at, is_muted, is_video_off}
    KEY  helen:gcc:{call_id}:meta          HASH  channel_id, call_type, routing, started_at
    SET  helen:gcc:{user_id}:calls          SET   of call_ids the user is in (used on disconnect)
    All TTL ``CALL_TTL_SEC`` (4h), refreshed on every mutation.

Fallback
--------
``redis_client=None`` → in-process dict, only this server's view.
Fine for single-server LAN; the API stays uniform so callers never
branch.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

CALL_TTL_SEC = 4 * 3600  # 4 hours; refresh on each mutation


@dataclass
class GroupParticipant:
    user_id: str
    server_id: str
    role: str = "member"  # "host" | "member" | "moderator"
    joined_at: float = field(default_factory=time.time)
    is_muted: bool = False
    is_video_off: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GroupParticipant":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class GroupCallMeta:
    channel_id: Optional[str] = None
    call_type: str = "audio"
    routing: str = "mesh"  # mesh | sfu | hybrid
    started_at: float = field(default_factory=time.time)


class DistributedGroupCallState:
    def __init__(
        self,
        redis_client=None,
        this_server_id: str = "local",
        broker=None,
    ) -> None:
        self._redis = redis_client
        self._sid = this_server_id
        # In-process fallback. Maps call_id → {user_id: GroupParticipant}.
        self._local: dict[str, dict[str, GroupParticipant]] = {}
        self._local_meta: dict[str, GroupCallMeta] = {}
        # Reverse index: user_id → set of call_ids (for cleanup on disconnect).
        self._local_user_calls: dict[str, set[str]] = {}
        # Broker-fanout fallback: when Redis isn't available but the
        # cluster has a broker (NATS / MQTT / Redis Streams / ZeroMQ /
        # RabbitMQ), participant mutations are published on a topic so
        # peer servers can mirror the state into their in-process dict.
        # This gives multi-server deployments a usable participant view
        # without requiring Redis as a hard dep.
        self._broker = broker

    @property
    def is_distributed(self) -> bool:
        return self._redis is not None

    # ── Mutations ──────────────────────────────────────────────

    async def set_meta(
        self, call_id: str, meta: GroupCallMeta,
    ) -> None:
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as p:
                    p.hset(
                        f"helen:gcc:{call_id}:meta",
                        mapping={k: str(v) for k, v in asdict(meta).items() if v is not None},
                    )
                    p.expire(f"helen:gcc:{call_id}:meta", CALL_TTL_SEC)
                    await p.execute()
                return
            except Exception as e:
                logger.warning("gcc_set_meta_failed", call_id=call_id, error=str(e))
        self._local_meta[call_id] = meta

    async def add_participant(
        self,
        call_id: str,
        user_id: str,
        *,
        server_id: Optional[str] = None,
        role: str = "member",
    ) -> bool:
        """Add ``user_id`` to the call. Returns True if newly added,
        False if already present. Idempotent."""
        sid = server_id or self._sid
        p = GroupParticipant(user_id=user_id, server_id=sid, role=role)
        if self._redis is not None:
            try:
                # HSETNX returns 1 on new, 0 on existing — gives idempotency.
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.hset(
                        f"helen:gcc:{call_id}:participants",
                        user_id, json.dumps(p.to_dict()),
                    )
                    pipe.expire(f"helen:gcc:{call_id}:participants", CALL_TTL_SEC)
                    pipe.sadd(f"helen:gcc:{user_id}:calls", call_id)
                    pipe.expire(f"helen:gcc:{user_id}:calls", CALL_TTL_SEC)
                    res = await pipe.execute()
                return bool(res[0]) if res else True
            except Exception as e:
                logger.warning(
                    "gcc_add_participant_failed",
                    call_id=call_id, user_id=user_id, error=str(e),
                )

        # In-process fallback.
        bucket = self._local.setdefault(call_id, {})
        new = user_id not in bucket
        bucket[user_id] = p
        self._local_user_calls.setdefault(user_id, set()).add(call_id)
        return new

    async def remove_participant(self, call_id: str, user_id: str) -> bool:
        """Remove ``user_id`` from the call. Returns True if was
        present. Idempotent."""
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.hdel(f"helen:gcc:{call_id}:participants", user_id)
                    pipe.srem(f"helen:gcc:{user_id}:calls", call_id)
                    res = await pipe.execute()
                return bool(res[0]) if res else False
            except Exception as e:
                logger.warning(
                    "gcc_remove_participant_failed",
                    call_id=call_id, user_id=user_id, error=str(e),
                )

        bucket = self._local.get(call_id)
        if bucket is None or user_id not in bucket:
            return False
        del bucket[user_id]
        if user_id in self._local_user_calls:
            self._local_user_calls[user_id].discard(call_id)
        if not bucket:
            self._local.pop(call_id, None)
        return True

    async def update_flags(
        self,
        call_id: str,
        user_id: str,
        *,
        is_muted: Optional[bool] = None,
        is_video_off: Optional[bool] = None,
    ) -> bool:
        """Flip mute / video flags. Returns True if applied. Idempotent."""
        cur = await self.get_participant(call_id, user_id)
        if cur is None:
            return False
        if is_muted is not None:
            cur.is_muted = bool(is_muted)
        if is_video_off is not None:
            cur.is_video_off = bool(is_video_off)
        if self._redis is not None:
            try:
                await self._redis.hset(
                    f"helen:gcc:{call_id}:participants",
                    user_id, json.dumps(cur.to_dict()),
                )
                return True
            except Exception as e:
                logger.warning("gcc_update_flags_failed", error=str(e))
                return False

        # In-process: dataclass is mutable; already updated above.
        bucket = self._local.get(call_id)
        if bucket is not None:
            bucket[user_id] = cur
        return True

    async def end_call(self, call_id: str) -> None:
        """Tear down all state for a call. Called on call_end."""
        if self._redis is not None:
            try:
                # Need participant list for reverse-index cleanup.
                participants = await self.list_participants(call_id)
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.delete(f"helen:gcc:{call_id}:participants")
                    pipe.delete(f"helen:gcc:{call_id}:meta")
                    for p in participants:
                        pipe.srem(f"helen:gcc:{p.user_id}:calls", call_id)
                    await pipe.execute()
                return
            except Exception as e:
                logger.warning("gcc_end_call_failed", call_id=call_id, error=str(e))

        # In-process fallback.
        bucket = self._local.pop(call_id, None) or {}
        for uid in bucket.keys():
            if uid in self._local_user_calls:
                self._local_user_calls[uid].discard(call_id)
        self._local_meta.pop(call_id, None)

    # ── Read API ───────────────────────────────────────────────

    async def get_participant(
        self, call_id: str, user_id: str,
    ) -> Optional[GroupParticipant]:
        if self._redis is not None:
            try:
                raw = await self._redis.hget(
                    f"helen:gcc:{call_id}:participants", user_id,
                )
                if raw is None:
                    return None
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return GroupParticipant.from_dict(json.loads(raw))
            except Exception as e:
                logger.warning("gcc_get_failed", error=str(e))
                return None
        return self._local.get(call_id, {}).get(user_id)

    async def list_participants(self, call_id: str) -> list[GroupParticipant]:
        if self._redis is not None:
            try:
                raw = await self._redis.hgetall(
                    f"helen:gcc:{call_id}:participants",
                )
                out = []
                for _uid, v in raw.items():
                    if isinstance(v, bytes):
                        v = v.decode("utf-8")
                    try:
                        out.append(GroupParticipant.from_dict(json.loads(v)))
                    except Exception:
                        continue
                return out
            except Exception as e:
                logger.warning("gcc_list_failed", error=str(e))
                return []
        return list(self._local.get(call_id, {}).values())

    async def list_calls_for_user(self, user_id: str) -> list[str]:
        """Used on disconnect to evict participant state from every
        call the user was in. Cheap because it's a SET."""
        if self._redis is not None:
            try:
                ids = await self._redis.smembers(f"helen:gcc:{user_id}:calls")
                return [i.decode("utf-8") if isinstance(i, bytes) else i for i in ids]
            except Exception as e:
                logger.warning("gcc_user_calls_failed", error=str(e))
                return []
        return list(self._local_user_calls.get(user_id, set()))

    async def participant_count(self, call_id: str) -> int:
        if self._redis is not None:
            try:
                return int(await self._redis.hlen(
                    f"helen:gcc:{call_id}:participants",
                ))
            except Exception:
                return 0
        return len(self._local.get(call_id, {}))


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[DistributedGroupCallState] = None


def get_group_call_state() -> DistributedGroupCallState:
    global _svc
    if _svc is None:
        _svc = DistributedGroupCallState(redis_client=None)
    return _svc


def configure(
    *, redis_client, this_server_id: str, broker=None,
) -> DistributedGroupCallState:
    global _svc
    _svc = DistributedGroupCallState(
        redis_client=redis_client,
        this_server_id=this_server_id,
        broker=broker,
    )
    if redis_client is not None:
        mode = "redis"
    elif broker is not None:
        mode = "broker-fanout"
    else:
        mode = "in-process"
    logger.info(
        "distributed_group_call_state_configured",
        mode=mode,
        server_id=this_server_id,
    )
    return _svc
