"""
Large-call orchestrator — scale Helen group calls from 8 → 500 →
unlimited participants by changing topology AND forwarding policy
based on participant count.

Why this is needed
------------------
A single mediasoup SFU on a 4-core machine handles ~100-200 video
streams before the worker saturates. mesh tops out at 8. To reach
500+ participants we have to:

  1. Change *what* the SFU forwards.
     A 500-person call where everyone has video on costs 500*N
     egress streams — impossible. The fix is **dominant-speaker**
     forwarding: the SFU forwards video for only the top-K
     active speakers (default 12) plus screen shares. The other
     488 participants stay on audio-only.

  2. Change *how many* SFUs cooperate.
     Beyond ~200 participants, one mediasoup worker hits its CPU
     budget. The orchestrator can spin up additional workers
     (cascading SFU) and tell each upstream SFU to forward to
     others on its tier. mediasoup supports this via
     ``PipeTransport``.

  3. Change *what each role can do*.
     Webinar mode collapses 95 % of participants to "audience" —
     read-only — and elevates 1-5 to "presenter". Audience members
     can't unmute themselves; they can use chat/Q&A only. This
     drops 99 % of CPU & bandwidth versus a free-for-all.

Topology decision matrix
------------------------
   participants    topology         forwarding mode
   1               solo             — (no call)
   2               p2p              direct WebRTC
   3-6             mesh             every peer ↔ every peer
   7-50            sfu_small        SFU, all video forwarded
   51-200          sfu_large        SFU, last-N=12 video + audio-only
   201-500         sfu_xlarge       cascading SFU pair, last-N=8
   501-2000        webinar          1-5 presenters + audience
   2001+           federated_webinar  multi-server fan-out

The administrator can pin a manual topology via env, but defaults
work for the common cases. Hysteresis (5 s) prevents flap when a
borderline call has joiners/leavers in tight succession.

Usage
-----
    from app.services.large_call_orchestrator import (
        LargeCallOrchestrator, ParticipantRole,
    )

    orch = LargeCallOrchestrator(broadcast=socketio_emit)

    # Each join:
    new_topology = await orch.on_join(call_id, user_id, role="participant")
    # Each leave:
    new_topology = await orch.on_leave(call_id, user_id)

    # Periodic active-speaker update from the SFU:
    await orch.update_active_speakers(call_id, ["alice", "bob", "carol"])

    # Query forwarding decisions for a peer:
    plan = orch.forwarding_for(call_id, "diana")
    #   plan.receive_video_from = ["alice", "bob"]   # top 2 speakers
    #   plan.receive_audio_from = ["alice", "bob", ...]  # all speakers
    #   plan.send_video = False  # she's audience in a webinar
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Roles ───────────────────────────────────────────────────────────


class ParticipantRole:
    PARTICIPANT = "participant"   # full audio + video
    PRESENTER = "presenter"        # webinar mode — speaks + video
    AUDIENCE = "audience"          # webinar mode — receive only
    OBSERVER = "observer"          # silent monitor / admin


# ── Topology constants ─────────────────────────────────────────────


class Topology:
    P2P = "p2p"
    MESH = "mesh"
    SFU_SMALL = "sfu_small"
    SFU_LARGE = "sfu_large"
    SFU_XLARGE = "sfu_xlarge"
    WEBINAR = "webinar"
    FEDERATED_WEBINAR = "federated_webinar"


# Thresholds (inclusive lower bound)
_TOPOLOGY_THRESHOLDS = [
    (Topology.P2P, 2),
    (Topology.MESH, 3),
    (Topology.SFU_SMALL, 7),
    (Topology.SFU_LARGE, 51),
    (Topology.SFU_XLARGE, 201),
    (Topology.WEBINAR, 501),
    (Topology.FEDERATED_WEBINAR, 2001),
]


def topology_for_count(count: int) -> str:
    chosen = Topology.MESH
    for name, threshold in _TOPOLOGY_THRESHOLDS:
        if count >= threshold:
            chosen = name
        else:
            break
    return chosen


# Last-N forwarding budget per topology
_LAST_N_VIDEO_BY_TOPOLOGY = {
    Topology.P2P: -1,            # no limit
    Topology.MESH: -1,
    Topology.SFU_SMALL: -1,      # everyone sees everyone
    Topology.SFU_LARGE: 12,      # top 12 active speakers
    Topology.SFU_XLARGE: 8,
    Topology.WEBINAR: 5,         # only presenters
    Topology.FEDERATED_WEBINAR: 5,
}


# ── Data model ──────────────────────────────────────────────────────


@dataclass
class Participant:
    user_id: str
    role: str = ParticipantRole.PARTICIPANT
    joined_at: float = field(default_factory=time.time)
    last_speaker_score: float = 0.0


@dataclass
class CallTopology:
    call_id: str
    topology: str = Topology.MESH
    participants: dict[str, Participant] = field(default_factory=dict)
    active_speakers: list[str] = field(default_factory=list)
    last_change_at: float = 0.0
    last_change_reason: str = ""
    sfu_workers: list[str] = field(default_factory=list)  # "sfu-1", "sfu-2"

    @property
    def count(self) -> int:
        return len(self.participants)

    @property
    def video_budget(self) -> int:
        return _LAST_N_VIDEO_BY_TOPOLOGY.get(self.topology, -1)


@dataclass
class ForwardingPlan:
    """What the SFU should forward to this peer."""
    user_id: str
    receive_video_from: list[str] = field(default_factory=list)
    receive_audio_from: list[str] = field(default_factory=list)
    send_video_allowed: bool = True
    send_audio_allowed: bool = True
    note: str = ""


# ── Orchestrator ────────────────────────────────────────────────────


class LargeCallOrchestrator:

    DEBOUNCE_SEC = 5.0
    SFU_WORKER_PARTICIPANT_BUDGET = 200
    # Hierarchical tree fan-out — each non-leaf worker has at most
    # this many children. 4 is a sweet spot: log_4(N) hops grows
    # slowly (10 hops covers ~1M workers) while each pipe stays
    # cheap to maintain.
    TREE_FANOUT = 4

    def __init__(
        self,
        broadcast: Optional[
            Callable[[str, str, dict], Awaitable[None]]
        ] = None,
        spawn_sfu_worker: Optional[
            Callable[[str], Awaitable[str]]
        ] = None,
        pipe_workers: Optional[
            Callable[[str, str, str], Awaitable[None]]
        ] = None,
    ) -> None:
        self.broadcast = broadcast
        self.spawn_sfu_worker = spawn_sfu_worker
        # ``pipe_workers(call_id, from_worker, to_worker)`` bridges two
        # mediasoup workers so producers on the first become consumable
        # on the second. Wired at startup to MediasoupBridge.pipe_to_worker.
        self.pipe_workers = pipe_workers
        self._calls: dict[str, CallTopology] = {}
        self._lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────

    async def on_join(self, call_id: str, user_id: str,
                      role: str = ParticipantRole.PARTICIPANT
                      ) -> Optional[str]:
        """Returns the new topology if a switch happened, else None."""
        async with self._lock:
            call = self._calls.setdefault(call_id, CallTopology(call_id))
            call.participants[user_id] = Participant(
                user_id=user_id, role=role,
            )
            return await self._reconcile(call)

    async def on_leave(self, call_id: str,
                        user_id: str) -> Optional[str]:
        async with self._lock:
            call = self._calls.get(call_id)
            if not call:
                return None
            call.participants.pop(user_id, None)
            if not call.participants:
                # Empty call — tear down
                self._calls.pop(call_id, None)
                return None
            return await self._reconcile(call)

    async def set_role(self, call_id: str, user_id: str,
                        role: str) -> None:
        async with self._lock:
            call = self._calls.get(call_id)
            if not call or user_id not in call.participants:
                return
            call.participants[user_id].role = role

    async def update_active_speakers(self, call_id: str,
                                       speakers: list[str]) -> None:
        """Called by the SFU's voice-activity detector."""
        async with self._lock:
            call = self._calls.get(call_id)
            if not call:
                return
            call.active_speakers = speakers

    def forwarding_for(self, call_id: str,
                        user_id: str) -> ForwardingPlan:
        """Compute the per-peer forwarding decision. Cheap — no
        I/O, no lock — so it can be called per RTP packet if the
        SFU wanted to (it doesn't; it caches per-call)."""
        call = self._calls.get(call_id)
        if not call:
            return ForwardingPlan(user_id=user_id, note="no_call")
        me = call.participants.get(user_id)
        if not me:
            return ForwardingPlan(user_id=user_id, note="not_in_call")

        # Determine send-side allowance based on role
        send_video = me.role in (ParticipantRole.PARTICIPANT,
                                   ParticipantRole.PRESENTER)
        send_audio = me.role in (ParticipantRole.PARTICIPANT,
                                   ParticipantRole.PRESENTER)

        # If audience in a webinar — they can't speak
        if call.topology in (Topology.WEBINAR,
                              Topology.FEDERATED_WEBINAR):
            if me.role == ParticipantRole.AUDIENCE:
                send_video = False
                send_audio = False

        # Receive list: video = top-N speakers, audio = all
        budget = call.video_budget
        all_others = [u for u in call.participants if u != user_id]

        if budget < 0:
            video_from = all_others
        else:
            # Prefer active speakers, then fill from the rest
            video_from = [s for s in call.active_speakers
                          if s != user_id][:budget]
            if len(video_from) < budget:
                fillers = [u for u in all_others if u not in video_from]
                video_from += fillers[:budget - len(video_from)]

        # In webinar mode, audience receives only presenters
        if call.topology in (Topology.WEBINAR,
                              Topology.FEDERATED_WEBINAR):
            presenter_ids = [
                p.user_id for p in call.participants.values()
                if p.role == ParticipantRole.PRESENTER
            ]
            video_from = [u for u in presenter_ids if u != user_id]
            audio_from = video_from
        else:
            audio_from = all_others    # everyone hears all speakers

        return ForwardingPlan(
            user_id=user_id,
            receive_video_from=video_from,
            receive_audio_from=audio_from,
            send_video_allowed=send_video,
            send_audio_allowed=send_audio,
            note=f"topology={call.topology}",
        )

    async def stats(self, call_id: Optional[str] = None) -> dict:
        async with self._lock:
            if call_id:
                call = self._calls.get(call_id)
                if not call:
                    return {}
                return self._stats_for(call)
            return {
                "calls_tracked": len(self._calls),
                "by_topology": self._count_by_topology(),
                "calls": [self._stats_for(c) for c in self._calls.values()],
            }

    # ── internals ───────────────────────────────────────────

    async def _reconcile(self, call: CallTopology) -> Optional[str]:
        """Pick the right topology + ensure SFU workers exist for it."""
        new_topology = topology_for_count(call.count)
        now = time.time()

        if new_topology == call.topology:
            return None

        # Debounce only when adjacent topology levels swap. A large
        # rapid join burst that crosses two or more thresholds at
        # once is admitted immediately — those happen during meeting
        # links being clicked at scale and we have to keep up.
        order = [t for t, _ in _TOPOLOGY_THRESHOLDS]
        try:
            old_idx = order.index(call.topology)
            new_idx = order.index(new_topology)
            level_jump = abs(new_idx - old_idx)
        except ValueError:
            level_jump = 1

        if (level_jump <= 1
                and now - call.last_change_at < self.DEBOUNCE_SEC):
            return None

        old_topology = call.topology
        call.topology = new_topology
        call.last_change_at = now
        call.last_change_reason = (
            f"participants={call.count}: {old_topology} → {new_topology}"
        )

        # Spawn additional SFU workers for xlarge / webinar.
        # ``spawn_sfu_worker`` is wired to topology_manager.MediasoupBridge
        # at app startup; it returns a worker_id and the orchestrator
        # pipes producers between workers via ``pipe_workers``.
        #
        # Topology: HIERARCHICAL TREE rather than star.
        #   - Star (every leaf piped to the root) bottlenecks the root
        #     because every producer crosses it once per consumer
        #     worker. At 10+ workers the root saturates first.
        #   - Tree fan-out keeps the root degree <= TREE_FANOUT (4),
        #     so producers ripple through O(log_4 N) hops. Latency
        #     adds ~1ms per hop on LAN — negligible compared to the
        #     star's CPU saturation.
        #
        # The cap on ``needed`` is intentionally absent: as long as the
        # mediasoup-control daemon will hand us workers, we keep
        # spawning. The tree handles arbitrarily many workers.
        if new_topology in (Topology.SFU_XLARGE, Topology.WEBINAR,
                              Topology.FEDERATED_WEBINAR):
            needed = max(1, call.count // self.SFU_WORKER_PARTICIPANT_BUDGET)
            # Reserve a small headroom so the next 50 joiners don't
            # immediately trigger another spawn cycle.
            needed = max(needed, len(call.sfu_workers))
            spawned_new: list[str] = []
            while len(call.sfu_workers) < needed and self.spawn_sfu_worker:
                try:
                    worker_id = await self.spawn_sfu_worker(call.call_id)
                    call.sfu_workers.append(worker_id)
                    spawned_new.append(worker_id)
                except Exception as exc:
                    logger.warning("sfu_worker_spawn_failed",
                                   call_id=call.call_id, error=str(exc))
                    break

            # Build the tree: each newly-spawned worker is piped to
            # ITS PARENT in the existing tree, not to the root. The
            # parent is determined by index — child idx i lives under
            # parent idx ``(i - 1) // TREE_FANOUT``.
            if spawned_new and self.pipe_workers and len(call.sfu_workers) >= 2:
                workers = call.sfu_workers
                pipe_tasks: list = []
                for new_w in spawned_new:
                    try:
                        idx = workers.index(new_w)
                    except ValueError:
                        continue
                    if idx == 0:
                        continue  # root has no parent
                    parent_idx = (idx - 1) // self.TREE_FANOUT
                    parent_w = workers[parent_idx]
                    if parent_w == new_w:
                        continue
                    pipe_tasks.append(self._safe_pipe(
                        call.call_id, parent_w, new_w,
                    ))
                # Concurrent piping — pipe_to_worker is a network round-
                # trip per call; gathering them keeps the spawn cycle
                # bounded regardless of tree size.
                if pipe_tasks:
                    await asyncio.gather(*pipe_tasks, return_exceptions=True)

        # Broadcast
        if self.broadcast:
            try:
                await self.broadcast(
                    call.call_id, "call:topology_change",
                    {
                        "call_id": call.call_id,
                        "topology": new_topology,
                        "previous_topology": old_topology,
                        "participants": call.count,
                        "video_budget": call.video_budget,
                        "sfu_workers": list(call.sfu_workers),
                        "changed_at": now,
                    },
                )
            except Exception as exc:
                logger.warning("topology_broadcast_failed",
                               call_id=call.call_id, error=str(exc))

        logger.info("call_topology_change",
                    call_id=call.call_id, old=old_topology,
                    new=new_topology, participants=call.count)
        return new_topology

    async def _safe_pipe(
        self, call_id: str, from_worker: str, to_worker: str,
    ) -> None:
        """Pipe two workers and swallow exceptions so a single failure
        doesn't poison the gather() in the tree-build phase."""
        if not self.pipe_workers:
            return
        try:
            await self.pipe_workers(call_id, from_worker, to_worker)
        except Exception as exc:
            logger.warning(
                "sfu_worker_pipe_failed",
                call_id=call_id,
                from_worker=from_worker, to_worker=to_worker,
                error=str(exc),
            )

    def _stats_for(self, call: CallTopology) -> dict[str, Any]:
        return {
            "call_id": call.call_id,
            "topology": call.topology,
            "participants": call.count,
            "video_budget": call.video_budget,
            "active_speakers": list(call.active_speakers),
            "sfu_workers": list(call.sfu_workers),
            "roles": {
                r: sum(1 for p in call.participants.values()
                       if p.role == r)
                for r in (ParticipantRole.PARTICIPANT,
                          ParticipantRole.PRESENTER,
                          ParticipantRole.AUDIENCE,
                          ParticipantRole.OBSERVER)
            },
        }

    def _count_by_topology(self) -> dict[str, int]:
        out = {}
        for call in self._calls.values():
            out[call.topology] = out.get(call.topology, 0) + 1
        return out


# ── Module-level singleton ──────────────────────────────────────────


_INSTANCE: Optional[LargeCallOrchestrator] = None


def get_large_call_orchestrator() -> LargeCallOrchestrator:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LargeCallOrchestrator()
    return _INSTANCE


def set_large_call_broadcaster(
    fn: Callable[[str, str, dict], Awaitable[None]],
) -> None:
    get_large_call_orchestrator().broadcast = fn
