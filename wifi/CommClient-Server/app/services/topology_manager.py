"""
Hybrid call topology manager.

Problem
-------
Mesh P2P scales like O(n²): every participant opens a RTCPeerConnection to
every other participant. At 8 users we already have 28 bidirectional peer
connections consuming upload bandwidth on every client. On a home WiFi
upload link (5–20 Mbps) this collapses at ~4 participants for video and
~6 for audio.

Solution
--------
Automatically switch between three routing modes based on participant count,
aggregate bandwidth estimate, and call type:

    ┌──────────┬──────────────┬──────────────────────────────────────────┐
    │ mode     │ trigger      │ behavior                                 │
    ├──────────┼──────────────┼──────────────────────────────────────────┤
    │ p2p      │ n == 2       │ direct RTCPeerConnection, no relay       │
    │ mesh     │ 3 ≤ n ≤ MESH │ full mesh between all participants       │
    │ sfu      │ n > MESH     │ every client publishes to the SFU and    │
    │          │              │ subscribes to the other producers        │
    │ hybrid   │ unstable net │ SFU fallback if mesh quality below floor │
    └──────────┴──────────────┴──────────────────────────────────────────┘

Thresholds are dynamic — the manager consults :class:`QualityOracle` which
aggregates the last quality reports from participants. When more than
``QUALITY_BAD_RATIO`` of participants report packet-loss above
``PACKET_LOSS_FLOOR``, topology is forcibly upgraded to SFU even if the
participant count is small.

SFU adapter
-----------
For now the SFU is a pluggable backend — a ``NoopSFU`` stub for development
and a :class:`MediasoupBridge` that speaks the mediasoup-worker IPC protocol.
Swapping backends is a config change.

Frontend contract
-----------------
Server emits ``topology_switch`` with:
    {
        "call_id": "...",
        "new_routing": "mesh" | "sfu" | "p2p",
        "generation": 3,
        "sfu": { "url": "...", "producer_token": "..." } | null,
        "reason": "participant_count" | "quality_floor" | "manual"
    }
Clients must:
 1. Ack the new generation via ``call_topology_ack``
 2. Drop every ``RTCPeerConnection`` whose ``topology_generation`` is smaller
 3. Re-negotiate according to ``new_routing``
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Tunable thresholds ──────────────────────────────────────────────────────
# Browser clients (the iOS web sim) don't ship the mediasoup adapter
# the desktop client uses, so they can't follow the server when it
# switches a call to SFU. We raise the mesh ceiling to 8 — the point
# at which O(n²) peer connections start to become genuinely painful in
# a browser tab — so all-browser group calls of normal size keep
# working without an SFU. Desktop ↔ desktop calls beyond this still
# upgrade to SFU automatically because the desktop client opts in.
# Lowered from 8 → 4 (audit fix 2.7) so SFU is exercised on every
# realistic group call instead of being dev-only "9+" theatre. Operators
# who prefer mesh up to 8 can opt back via env. Hard floor 2 so a P2P
# call doesn't accidentally promote.
import os as _os_mesh_max
try:
    _env_mesh_max = int(_os_mesh_max.environ.get("HELEN_MESH_MAX_PARTICIPANTS", "4"))
except ValueError:
    _env_mesh_max = 4
MESH_MAX_PARTICIPANTS = max(2, _env_mesh_max)
SFU_MIN_PARTICIPANTS = MESH_MAX_PARTICIPANTS + 1
PACKET_LOSS_FLOOR = 0.08           # 8% packet loss → upgrade to SFU
QUALITY_BAD_RATIO = 0.4            # 40% of participants reporting bad → switch
COOLDOWN_SECONDS = 15              # prevent topology flapping
RTT_BAD_MS = 400                   # one-way RTT above this → upgrade


# ─────────────────────────────────────────────────────────────────────────────
# SFU backend interface
# ─────────────────────────────────────────────────────────────────────────────

class SFUBackend:
    """Interface every SFU adapter must implement."""

    name: str = "noop"

    async def allocate_router(self, call_id: str) -> dict[str, Any]:
        """Create a router/room and return {url, producer_token, ...}."""
        raise NotImplementedError

    async def release_router(self, call_id: str) -> None:
        raise NotImplementedError


class NoopSFU(SFUBackend):
    """Fallback when no real SFU is configured. Returns mesh info instead."""

    name = "noop"

    async def allocate_router(self, call_id: str) -> dict[str, Any]:
        return {
            "backend": "noop",
            "url": None,
            "producer_token": None,
            "note": "No SFU configured; topology manager will keep mesh mode.",
        }

    async def release_router(self, call_id: str) -> None:  # pragma: no cover
        return None


class MediasoupBridge(SFUBackend):
    """
    Thin client for an external mediasoup Node worker running on localhost.
    The worker exposes an HTTP control API (not the browser-side protocol).
    Expected ENV: MEDIASOUP_CONTROL_URL, MEDIASOUP_CONTROL_TOKEN.

    This class only talks to 127.0.0.1 — nothing leaves the LAN.
    """

    name = "mediasoup"

    def __init__(self, control_url: str, token: str | None = None) -> None:
        self.control_url = control_url.rstrip("/")
        self.token = token
        self._client: Any = None  # lazy httpx.AsyncClient
        self._client_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _http(self):
        """Lazy-init a shared AsyncClient — avoids the per-call handshake cost
        which became visible once the SFU endpoints started getting hit at the
        per-transport / per-consumer granularity (often tens of req / sec per call)."""
        import httpx
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self.control_url,
                        headers=self._headers(),
                        timeout=httpx.Timeout(10.0, connect=3.0),
                        limits=httpx.Limits(
                            max_connections=32, max_keepalive_connections=16,
                        ),
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    # ── SFUBackend interface ────────────────────────────────────────────────

    async def allocate_router(self, call_id: str) -> dict[str, Any]:
        client = await self._http()
        r = await client.post("/routers", json={"call_id": call_id})
        r.raise_for_status()
        data = r.json()
        return {
            "backend": "mediasoup",
            "url": data.get("url"),
            "producer_token": data.get("token"),
            "rtp_capabilities": data.get("rtp_capabilities"),
            "transport_options": data.get("transport_options"),
        }

    async def release_router(self, call_id: str) -> None:
        try:
            client = await self._http()
            await client.delete(f"/routers/{call_id}")
        except Exception as exc:  # pragma: no cover
            logger.warning("mediasoup_release_failed", call_id=call_id, error=str(exc))

    # ── Extended: transport + produce + consume plumbing ────────────────────

    async def create_transport(
        self, call_id: str, peer_id: str, direction: str,
    ) -> dict[str, Any]:
        if direction not in {"send", "recv"}:
            raise ValueError("direction must be 'send' or 'recv'")
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/transports",
            json={"peer_id": peer_id, "direction": direction},
        )
        r.raise_for_status()
        return r.json()

    async def connect_transport(
        self, call_id: str, transport_id: str, dtls_parameters: dict[str, Any],
    ) -> None:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/transports/{transport_id}/connect",
            json={"dtls_parameters": dtls_parameters},
        )
        r.raise_for_status()

    async def produce(
        self,
        call_id: str,
        transport_id: str,
        kind: str,
        rtp_parameters: dict[str, Any],
        app_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"audio", "video"}:
            raise ValueError("kind must be 'audio' or 'video'")
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/transports/{transport_id}/produce",
            json={
                "kind": kind,
                "rtp_parameters": rtp_parameters,
                "app_data": app_data or {},
            },
        )
        r.raise_for_status()
        return r.json()

    async def consume(
        self,
        call_id: str,
        transport_id: str,
        producer_id: str,
        peer_id: str,
        rtp_capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/consume",
            json={
                "transport_id": transport_id,
                "producer_id": producer_id,
                "peer_id": peer_id,
                "rtp_capabilities": rtp_capabilities,
            },
        )
        r.raise_for_status()
        return r.json()

    async def resume_consumer(self, call_id: str, consumer_id: str) -> None:
        client = await self._http()
        r = await client.post(f"/routers/{call_id}/consumers/{consumer_id}/resume")
        r.raise_for_status()

    async def pause_consumer(self, call_id: str, consumer_id: str) -> None:
        client = await self._http()
        r = await client.post(f"/routers/{call_id}/consumers/{consumer_id}/pause")
        r.raise_for_status()

    async def peer_leave(self, call_id: str, peer_id: str) -> None:
        try:
            client = await self._http()
            await client.post(f"/routers/{call_id}/peers/{peer_id}/leave")
        except Exception as exc:  # pragma: no cover
            logger.warning("mediasoup_peer_leave_failed", call_id=call_id, peer_id=peer_id, error=str(exc))

    # ── Bandwidth / simulcast controls ──────────────────────────────────────

    async def set_preferred_layers(
        self,
        call_id: str,
        consumer_id: str,
        spatial_layer: int,
        temporal_layer: int | None = None,
    ) -> None:
        """Client-driven simulcast/SVC layer selection for a consumer."""
        client = await self._http()
        payload: dict[str, Any] = {"spatial_layer": int(spatial_layer)}
        if temporal_layer is not None:
            payload["temporal_layer"] = int(temporal_layer)
        r = await client.post(
            f"/routers/{call_id}/consumers/{consumer_id}/preferred-layers",
            json=payload,
        )
        r.raise_for_status()

    async def set_consumer_priority(
        self, call_id: str, consumer_id: str, priority: int,
    ) -> None:
        """Consumer bandwidth-allocation priority (1..255)."""
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/consumers/{consumer_id}/priority",
            json={"priority": int(priority)},
        )
        r.raise_for_status()

    async def set_max_incoming_bitrate(
        self, call_id: str, transport_id: str, bitrate: int,
    ) -> None:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/transports/{transport_id}/max-incoming-bitrate",
            json={"bitrate": int(bitrate)},
        )
        r.raise_for_status()

    async def set_max_outgoing_bitrate(
        self, call_id: str, transport_id: str, bitrate: int,
    ) -> None:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/transports/{transport_id}/max-outgoing-bitrate",
            json={"bitrate": int(bitrate)},
        )
        r.raise_for_status()

    # ── Producer pause / resume — driven by mute button ────────────────────

    async def pause_producer(self, call_id: str, producer_id: str) -> None:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/producers/{producer_id}/pause",
        )
        r.raise_for_status()

    async def resume_producer(self, call_id: str, producer_id: str) -> None:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/producers/{producer_id}/resume",
        )
        r.raise_for_status()

    # ── Active-speaker detection ───────────────────────────────────────────

    async def ensure_audio_observer(self, call_id: str) -> None:
        client = await self._http()
        r = await client.post(f"/routers/{call_id}/audio-observer/ensure")
        r.raise_for_status()

    async def audio_observer_add(self, call_id: str, producer_id: str) -> None:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/audio-observer/add",
            json={"producer_id": producer_id},
        )
        r.raise_for_status()

    async def audio_observer_remove(self, call_id: str, producer_id: str) -> None:
        try:
            client = await self._http()
            await client.post(
                f"/routers/{call_id}/audio-observer/remove",
                json={"producer_id": producer_id},
            )
        except Exception:  # pragma: no cover
            pass

    # ── Recording (PlainRtpTransport → ffmpeg) ─────────────────────────────

    async def start_recording(
        self,
        call_id: str,
        audio_producer_id: str | None = None,
        video_producer_id: str | None = None,
        recording_id: str | None = None,
    ) -> dict[str, Any]:
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/recording",
            json={
                "audio_producer_id": audio_producer_id,
                "video_producer_id": video_producer_id,
                "recording_id": recording_id,
            },
        )
        r.raise_for_status()
        return r.json()

    async def stop_recording(self, call_id: str, recording_id: str) -> dict[str, Any]:
        client = await self._http()
        r = await client.delete(f"/routers/{call_id}/recording/{recording_id}")
        r.raise_for_status()
        return r.json()

    async def list_recordings(self, call_id: str) -> dict[str, Any]:
        client = await self._http()
        r = await client.get(f"/routers/{call_id}/recordings")
        r.raise_for_status()
        return r.json()

    # ── Cascading SFU (PipeTransport between workers) ──────────────────────
    #
    # When a call exceeds one worker's CPU budget (~200 video peers), the
    # large-call orchestrator spawns additional workers and we need to
    # bridge the routers so a producer on worker-A is visible to consumers
    # on worker-B. mediasoup's standard mechanism for this is a router
    # pipe — a PlainRtpTransport pair that carries RTP between workers.
    #
    # Both endpoints below mirror the mediasoup Node API. The control
    # plane (this Python class) just shuttles dicts; the worker decides
    # the actual RTP listen IPs and ports.

    async def spawn_worker(self, call_id: str) -> dict[str, Any]:
        """Allocate an additional mediasoup worker for ``call_id``.

        Returns ``{worker_id, router_id, url}`` so the orchestrator can
        record it on ``CallTopology.sfu_workers``. The mediasoup-control
        node keeps a worker pool and creates a router on whichever
        worker is least loaded.
        """
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/workers",
            json={"call_id": call_id},
        )
        r.raise_for_status()
        return r.json()

    async def pipe_to_worker(
        self,
        call_id: str,
        from_worker: str,
        to_worker: str,
        producer_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Bridge two workers so producers on ``from_worker`` become
        consumable on ``to_worker`` via PipeTransport.

        Idempotent — if a pipe is already established the worker
        returns the existing pair. Pass ``producer_ids=None`` to pipe
        every existing producer; pass an explicit list to be selective
        (useful for cascading SFU with last-N forwarding).
        """
        client = await self._http()
        r = await client.post(
            f"/routers/{call_id}/pipes",
            json={
                "from_worker": from_worker,
                "to_worker": to_worker,
                "producer_ids": producer_ids,
            },
        )
        r.raise_for_status()
        return r.json()

    async def release_worker(self, call_id: str, worker_id: str) -> None:
        try:
            client = await self._http()
            await client.delete(f"/routers/{call_id}/workers/{worker_id}")
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "mediasoup_release_worker_failed",
                call_id=call_id, worker_id=worker_id, error=str(exc),
            )


def _make_default_backend() -> SFUBackend:
    import os
    url = os.environ.get("MEDIASOUP_CONTROL_URL")
    token = os.environ.get("MEDIASOUP_CONTROL_TOKEN")
    if url:
        return MediasoupBridge(url, token)
    return NoopSFU()


# ─────────────────────────────────────────────────────────────────────────────
# Quality oracle
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QualitySample:
    packet_loss: float = 0.0
    rtt_ms: float = 0.0
    jitter_ms: float = 0.0


class QualityOracle:
    """Tracks the last quality report per (call_id, user_id)."""

    def __init__(self) -> None:
        self._samples: dict[str, dict[str, QualitySample]] = {}

    def record(self, call_id: str, user_id: str, sample: QualitySample) -> None:
        self._samples.setdefault(call_id, {})[user_id] = sample

    def forget(self, call_id: str) -> None:
        self._samples.pop(call_id, None)

    def bad_participants_ratio(self, call_id: str) -> float:
        call = self._samples.get(call_id)
        if not call:
            return 0.0
        bad = 0
        for s in call.values():
            if s.packet_loss >= PACKET_LOSS_FLOOR or s.rtt_ms >= RTT_BAD_MS:
                bad += 1
        return bad / max(len(call), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Main topology manager
# ─────────────────────────────────────────────────────────────────────────────

class TopologyManager:
    def __init__(self, backend: SFUBackend | None = None) -> None:
        self._backend: SFUBackend = backend or _make_default_backend()
        self._generations: dict[str, int] = {}
        self._last_switch_ts: dict[str, float] = {}
        self._router_info: dict[str, dict[str, Any]] = {}
        self.quality = QualityOracle()

    # ── Public API ──────────────────────────────────────────────────────────

    def desired_routing(self, participant_count: int, call_id: str | None = None) -> str:
        """
        Pure function: what routing *should* be for this count (ignores quality).
        """
        if participant_count <= 1:
            return "p2p"      # self-only, e.g. ringing
        if participant_count == 2:
            return "p2p"
        if participant_count <= MESH_MAX_PARTICIPANTS:
            return "mesh"
        return "sfu"

    def should_upgrade_for_quality(self, call_id: str) -> bool:
        return self.quality.bad_participants_ratio(call_id) >= QUALITY_BAD_RATIO

    def should_use_hybrid(self, call_id: str, participant_count: int) -> bool:
        """
        Hybrid mode: small group (mesh range) where SOME participants report
        bad quality but the majority are fine. Promote only the degraded
        participants onto the SFU; everyone else stays on mesh. This avoids
        the all-or-nothing flap between mesh and full SFU.

        Trigger: 2 ≤ count ≤ MESH_MAX_PARTICIPANTS AND any participant is
        below the quality floor (but not enough to force full SFU upgrade).
        """
        if participant_count < 2 or participant_count > MESH_MAX_PARTICIPANTS:
            return False
        bad_ratio = self.quality.bad_participants_ratio(call_id)
        return 0.0 < bad_ratio < QUALITY_BAD_RATIO

    def degraded_participants(self, call_id: str) -> list[str]:
        """Return user_ids whose last quality sample is below the floor."""
        call = self.quality._samples.get(call_id) or {}
        out: list[str] = []
        for uid, s in call.items():
            if s.packet_loss >= PACKET_LOSS_FLOOR or s.rtt_ms >= RTT_BAD_MS:
                out.append(uid)
        return out

    async def reevaluate(self, call: Any) -> str | None:
        """
        Look at the current call, decide whether to change topology, and if so
        allocate/release SFU resources + emit ``topology_switch`` to all
        participants. Returns the new routing if it changed, else None.

        ``call`` is the in-memory :class:`app.services.call_service.ActiveCall`
        object (kept untyped here to avoid a circular import).
        """
        import time

        count = len(call.participants)
        desired = self.desired_routing(count, call.call_id)

        # Quality-driven override: small groups with bad links still get SFU.
        if desired in {"p2p", "mesh"} and self.should_upgrade_for_quality(call.call_id):
            desired = "sfu"
        # Hybrid: a few participants struggling but not enough to flap the
        # whole call onto the SFU — keep mesh, mark this generation hybrid
        # so the broadcaster can ship per-participant SFU hints.
        elif desired == "mesh" and self.should_use_hybrid(call.call_id, count):
            desired = "hybrid"

        if desired == call.routing:
            # Still push a heartbeat so persistence stays fresh.
            return None

        # Cooldown — don't flap between mesh and sfu.
        last = self._last_switch_ts.get(call.call_id, 0.0)
        if time.time() - last < COOLDOWN_SECONDS:
            return None

        return await self.force_switch(
            call, desired, reason=self._switch_reason(call, desired),
        )

    async def force_switch(
        self,
        call: Any,
        new_routing: str,
        *,
        reason: str = "manual",
    ) -> str:
        """Unconditional switch. Returns the new routing."""
        import time

        old_routing = call.routing
        call_id = call.call_id
        generation = self._generations.get(call_id, 1) + 1
        self._generations[call_id] = generation
        self._last_switch_ts[call_id] = time.time()

        # Allocate/release SFU backend if needed.
        # Hybrid mode also needs an SFU router (only degraded peers consume it).
        sfu_info: dict[str, Any] | None = None
        needs_sfu = new_routing in {"sfu", "hybrid"}
        if needs_sfu:
            try:
                sfu_info = await self._backend.allocate_router(call_id)
                self._router_info[call_id] = sfu_info
                if new_routing == "hybrid":
                    sfu_info = dict(sfu_info)
                    sfu_info["degraded_participants"] = self.degraded_participants(call_id)
            except Exception as exc:
                logger.error("sfu_allocate_failed", call_id=call_id, error=str(exc))
                # Downgrade to mesh if SFU allocation fails — keep call alive.
                new_routing = "mesh"
                sfu_info = None
        elif old_routing in {"sfu", "hybrid"} and new_routing not in {"sfu", "hybrid"}:
            try:
                await self._backend.release_router(call_id)
            except Exception:
                pass
            self._router_info.pop(call_id, None)

        call.routing = new_routing

        # Persist
        try:
            from app.services.call_state_persistence import call_state_persistence
            await call_state_persistence.bump_topology(
                call_id=call_id, new_routing=new_routing, generation=generation,
            )
            await call_state_persistence.append_signal(
                call_id=call_id,
                from_user="server",
                to_user=None,
                kind="topology",
                payload={
                    "old_routing": old_routing,
                    "new_routing": new_routing,
                    "generation": generation,
                    "reason": reason,
                    "sfu": sfu_info,
                },
                topology_generation=generation,
            )
        except Exception as exc:
            logger.error("topology_persist_failed", call_id=call_id, error=str(exc))

        # Broadcast to participants
        await self._broadcast_switch(
            call=call,
            new_routing=new_routing,
            generation=generation,
            sfu_info=sfu_info,
            reason=reason,
        )

        logger.info(
            "topology_switched",
            call_id=call_id,
            old=old_routing,
            new=new_routing,
            generation=generation,
            reason=reason,
            participants=len(call.participants),
        )
        return new_routing

    def current_generation(self, call_id: str) -> int:
        return self._generations.get(call_id, 1)

    def restore_generation(self, call_id: str, generation: int) -> None:
        """
        Called by :meth:`CallService.rehydrate_from_db` after restart so the
        generation counter for a live call does not rewind to 1. Clients keep
        their last-acked generation in memory; if the server starts counting
        from 1 after a restart, every subsequent ``topology_ack`` would carry
        a generation higher than ``self._generations[call_id]`` and the ack
        handler would reject them, triggering full-renegotiate storms.

        We take ``max(current, persisted)`` so repeated rehydrates are safe.
        """
        if generation < 1:
            return
        cur = self._generations.get(call_id, 1)
        if generation > cur:
            self._generations[call_id] = generation

    def mark_router_stale(self, call_id: str) -> None:
        """
        Called after server restart for calls that were in ``sfu`` routing
        mode. The mediasoup worker no longer holds a router for this call,
        so the next :meth:`reevaluate` must treat it as un-allocated and
        either re-allocate or downgrade to mesh. We just drop the cached
        ``_router_info`` — ``force_switch`` will call ``allocate_router``
        again on the next topology decision.
        """
        self._router_info.pop(call_id, None)

    async def on_call_ended(self, call_id: str) -> None:
        self._generations.pop(call_id, None)
        self._last_switch_ts.pop(call_id, None)
        self.quality.forget(call_id)
        info = self._router_info.pop(call_id, None)
        if info:
            try:
                await self._backend.release_router(call_id)
            except Exception:
                pass

    # ── Internal helpers ────────────────────────────────────────────────────

    def _switch_reason(self, call: Any, desired: str) -> str:
        if desired == "sfu" and call.routing in {"p2p", "mesh"}:
            if self.should_upgrade_for_quality(call.call_id):
                return "quality_floor"
            return "participant_count"
        if desired in {"mesh", "p2p"} and call.routing == "sfu":
            return "participant_count_downgrade"
        return "policy"

    async def _broadcast_switch(
        self,
        *,
        call: Any,
        new_routing: str,
        generation: int,
        sfu_info: dict[str, Any] | None,
        reason: str,
    ) -> None:
        """Emit ``topology_switch`` over Socket.IO to every active sid.

        Each participant receives their own ephemeral TURN credentials —
        after a topology switch clients typically tear down and rebuild
        RTCPeerConnections, so they need a fresh ICE server list.
        """
        try:
            from app.socket.server import sio
            from app.services.presence_service import presence_service
        except Exception:
            return

        # Build ICE config per user (credentials are user-scoped).
        try:
            from app.services.ice_config_service import build_ice_config
        except Exception:
            build_ice_config = None  # type: ignore

        base_payload = {
            "call_id": call.call_id,
            "new_routing": new_routing,
            "generation": generation,
            "sfu": sfu_info,
            "reason": reason,
        }

        async def _emit_one(sid: str, payload: dict[str, Any]) -> None:
            try:
                await sio.emit("topology_switch", payload, to=sid)
            except Exception:
                pass

        tasks: list[asyncio.Task] = []
        for user_id in list(call.participants.keys()):
            user_payload = dict(base_payload)
            if build_ice_config is not None:
                try:
                    cfg = build_ice_config(user_id)
                    user_payload["ice_servers"] = cfg["ice_servers"]
                    user_payload["ice_transport_policy"] = cfg["ice_transport_policy"]
                    user_payload["ice_ttl_seconds"] = cfg.get("ttl_seconds")
                except Exception as e:
                    logger.warning(
                        "topology_switch_ice_build_failed",
                        user_id=user_id,
                        error=str(e),
                    )
            try:
                sids = presence_service.get_sids(user_id) or []
            except Exception:
                sids = []
            # H-5 / part of BLOCKER-3: emit BOTH the legacy
            # ``topology_switch`` event AND the v2-named
            # ``call_topology_updated`` event. v1 clients keep
            # listening on the legacy name; v2 clients prefer the
            # explicit name. emit_to_user routes via federation when
            # the user has no local sid (fed-hosted participant).
            for sid in sids:
                tasks.append(asyncio.create_task(_emit_one(sid, user_payload)))
                async def _emit_v2(_sid: str, _payload: dict[str, Any]) -> None:
                    try:
                        await sio.emit("call_topology_updated", _payload, to=_sid)
                    except Exception:
                        pass
                tasks.append(asyncio.create_task(_emit_v2(sid, user_payload)))
            if not sids:
                try:
                    from app.socket.server import emit_to_user as _etu
                    tasks.append(asyncio.create_task(
                        _etu("call_topology_updated", user_payload, user_id)
                    ))
                    tasks.append(asyncio.create_task(
                        _etu("topology_switch", user_payload, user_id)
                    ))
                except Exception:
                    pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# Singleton
topology_manager = TopologyManager()
