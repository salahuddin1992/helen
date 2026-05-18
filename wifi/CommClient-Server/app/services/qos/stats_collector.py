"""
QoS Stats Collector — central rolling buffer for live call telemetry.

Architecture
------------
``call_handlers.py`` (the ~4,300-line socket layer) is **not** modified
directly. Instead, this module exposes a thin event-subscription hook
(`register_qos_observer`) and a public ``ingest_*`` API that the existing
``call_quality_report`` socket event already calls through ``call_service.
report_quality`` — we monkey-wrap that single method at import time so
*every* incoming metric also fans out to QoS without touching the file.

Storage
-------
For each (call_id, participant_id, stream_id) tuple we keep a 5-minute
rolling buffer of per-second snapshots in a ``deque(maxlen=300)``. That
gives us 300 samples × ~120 bytes ≈ 36 KB per stream, which scales to
thousands of concurrent streams on a single Helen node.

Event types ingested
--------------------
  stream_metrics    — getStats() aggregates from the client.
  rtcp_report       — SR/RR XR blocks parsed by client.
  jitter_buffer     — adaptive buffer state (target, current, emitted).
  bandwidth         — BWE / REMB estimates (send + recv).
  codec_switch      — codec adaptation event.
  preset_switch     — QoS preset change (e.g. HD→SD).
  fec_event         — FEC enabled/disabled + percentage.
  simulcast_event   — simulcast layer toggle.
  quality_event     — generic flagged quality incident.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from app.core.logging import get_logger
from app.services.qos.mos_calculator import MOSCalculator, qos_mos_calculator

logger = get_logger(__name__)


# ── Tunables ───────────────────────────────────────────────────────────
ROLLING_BUFFER_SECONDS = 300            # 5 min @1Hz
MAX_EVENTS_PER_CALL = 1000              # cap codec/preset/quality history
DEFAULT_SAMPLE_INTERVAL_S = 1.0


QoSObserver = Callable[[str, dict[str, Any]], Awaitable[None] | None]


# ── Per-stream rolling sample ──────────────────────────────────────────

@dataclass
class StreamSample:
    timestamp: float
    kind: str                   # "audio" | "video" | "screen"
    direction: str              # "inbound" | "outbound"
    codec: str | None
    bitrate_kbps: float
    packets_sent: int
    packets_lost: int
    packet_loss_pct: float
    jitter_ms: float
    rtt_ms: float
    fps: float | None
    resolution: str | None
    frames_dropped: int
    nack_count: int
    pli_count: int
    fir_count: int
    audio_level: float | None
    mos: float
    r_factor: float
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.timestamp,
            "kind": self.kind,
            "direction": self.direction,
            "codec": self.codec,
            "bitrate_kbps": round(self.bitrate_kbps, 2),
            "packets_sent": self.packets_sent,
            "packets_lost": self.packets_lost,
            "loss_pct": round(self.packet_loss_pct, 3),
            "jitter_ms": round(self.jitter_ms, 2),
            "rtt_ms": round(self.rtt_ms, 2),
            "fps": self.fps,
            "resolution": self.resolution,
            "frames_dropped": self.frames_dropped,
            "nack": self.nack_count,
            "pli": self.pli_count,
            "fir": self.fir_count,
            "audio_level": self.audio_level,
            "mos": round(self.mos, 3),
            "r_factor": round(self.r_factor, 2),
        }


# ── Per-call container ─────────────────────────────────────────────────

@dataclass
class CallTelemetry:
    call_id: str
    started_at: float = field(default_factory=time.time)
    # (participant_id, stream_id) → deque[StreamSample]
    streams: dict[tuple[str, str], deque[StreamSample]] = field(default_factory=dict)
    # participant_id → latest RTCP report dict
    rtcp_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    # participant_id → latest jitter-buffer state
    jitter_buffer: dict[str, dict[str, Any]] = field(default_factory=dict)
    # participant_id → {send_bps, recv_bps, available_bps}
    bandwidth: dict[str, dict[str, Any]] = field(default_factory=dict)
    # bounded event log (preset/codec/fec/simulcast/quality)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_CALL))


class QoSStatsCollector:
    """
    Singleton rolling-buffer that ingests live call metrics.

    The collector deliberately stays in-memory and per-process. Cross-worker
    aggregation is the job of the cluster service, not this hot path.
    """

    def __init__(self) -> None:
        self._calls: dict[str, CallTelemetry] = {}
        self._lock = asyncio.Lock()
        self._observers: list[QoSObserver] = []
        self._mos = qos_mos_calculator
        self._hooked_call_service = False

    # ── External observer API ──────────────────────────────────────────

    def register_qos_observer(self, observer: QoSObserver) -> None:
        """Subscribe to every ingested event. ``observer(event_type, payload)``."""
        self._observers.append(observer)

    async def _fanout(self, event_type: str, payload: dict[str, Any]) -> None:
        for obs in list(self._observers):
            try:
                ret = obs(event_type, payload)
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception as e:                  # pragma: no cover
                logger.warning("qos_observer_error", error=str(e), event=event_type)

    # ── Lazy hook into existing call_service.report_quality ────────────

    def attach_to_call_service(self) -> None:
        """
        Wrap ``call_service.report_quality`` once so any client posting
        ``call_quality_report`` over Socket.IO also feeds QoS without
        editing the original socket handler.
        """
        if self._hooked_call_service:
            return
        try:
            from app.services.call_service import call_service
            original = call_service.report_quality

            async def _wrapped(call_id: str, user_id: str, metrics: dict) -> None:
                await original(call_id, user_id, metrics)
                try:
                    await self.ingest_stream_metrics(
                        call_id, user_id, metrics or {},
                    )
                except Exception as e:              # pragma: no cover
                    logger.warning("qos_ingest_via_hook_failed", error=str(e))

            call_service.report_quality = _wrapped  # type: ignore[assignment]
            self._hooked_call_service = True
            logger.info("qos_collector_hooked_call_service")
        except Exception as e:                      # pragma: no cover
            logger.warning("qos_collector_hook_failed", error=str(e))

    # ── Ingestion API (called by hook or by tests) ─────────────────────

    async def ingest_stream_metrics(
        self,
        call_id: str,
        participant_id: str,
        metrics: dict[str, Any],
        stream_id: str | None = None,
    ) -> StreamSample:
        """
        Normalize a raw client getStats() metric dict into a ``StreamSample``
        and append to the rolling buffer.

        Required-ish keys (all optional, with sensible defaults):
            rtt_ms, jitter_ms, packet_loss (0-1 or 0-100), bandwidth_mbps,
            codec, video_fps, video_resolution, packets_sent, packets_lost,
            nack_count, pli_count, fir_count, audio_level, kind, direction.
        """
        async with self._lock:
            tele = self._calls.setdefault(call_id, CallTelemetry(call_id=call_id))

            kind = (metrics.get("kind") or metrics.get("media_type") or "audio").lower()
            direction = (metrics.get("direction") or "outbound").lower()
            stream_key = (participant_id, stream_id or f"{kind}:{direction}")

            # Accept loss either as 0-1 fraction or 0-100 percent.
            raw_loss = metrics.get("packet_loss")
            if raw_loss is None:
                raw_loss = metrics.get("loss_pct", 0.0)
            loss_pct = float(raw_loss or 0.0)
            if 0.0 <= loss_pct <= 1.0 and "packet_loss" in metrics:
                # Heuristic: treat fractional input as 0-1 → percent
                loss_pct *= 100.0

            jitter_ms = float(metrics.get("jitter_ms") or 0.0)
            rtt_ms = float(metrics.get("rtt_ms") or 0.0)
            codec = metrics.get("codec")
            bitrate_kbps = float(
                metrics.get("bitrate_kbps")
                or (float(metrics.get("bandwidth_mbps") or 0.0) * 1000.0)
            )

            mos_result = self._mos.compute_mos(jitter_ms, loss_pct, rtt_ms, codec)

            sample = StreamSample(
                timestamp=time.time(),
                kind=kind,
                direction=direction,
                codec=codec,
                bitrate_kbps=bitrate_kbps,
                packets_sent=int(metrics.get("packets_sent") or 0),
                packets_lost=int(metrics.get("packets_lost") or 0),
                packet_loss_pct=loss_pct,
                jitter_ms=jitter_ms,
                rtt_ms=rtt_ms,
                fps=metrics.get("video_fps") or metrics.get("fps"),
                resolution=metrics.get("video_resolution") or metrics.get("resolution"),
                frames_dropped=int(metrics.get("frames_dropped") or 0),
                nack_count=int(metrics.get("nack_count") or 0),
                pli_count=int(metrics.get("pli_count") or 0),
                fir_count=int(metrics.get("fir_count") or 0),
                audio_level=metrics.get("audio_level"),
                mos=mos_result.mos,
                r_factor=mos_result.r_factor,
                raw=metrics,
            )

            buf = tele.streams.get(stream_key)
            if buf is None:
                buf = deque(maxlen=ROLLING_BUFFER_SECONDS)
                tele.streams[stream_key] = buf
            buf.append(sample)

        await self._fanout("stream_metrics", {
            "call_id": call_id,
            "participant_id": participant_id,
            "stream_id": stream_key[1],
            "sample": sample.to_dict(),
        })
        return sample

    async def ingest_rtcp_report(
        self, call_id: str, participant_id: str, report: dict[str, Any],
    ) -> None:
        async with self._lock:
            tele = self._calls.setdefault(call_id, CallTelemetry(call_id=call_id))
            stamped = {**report, "ts": time.time()}
            tele.rtcp_reports[participant_id] = stamped
        await self._fanout("rtcp_report", {
            "call_id": call_id, "participant_id": participant_id, "report": stamped,
        })

    async def ingest_jitter_buffer(
        self, call_id: str, participant_id: str, state: dict[str, Any],
    ) -> None:
        async with self._lock:
            tele = self._calls.setdefault(call_id, CallTelemetry(call_id=call_id))
            tele.jitter_buffer[participant_id] = {**state, "ts": time.time()}
        await self._fanout("jitter_buffer", {
            "call_id": call_id, "participant_id": participant_id, "state": state,
        })

    async def ingest_bandwidth(
        self, call_id: str, participant_id: str, estimate: dict[str, Any],
    ) -> None:
        async with self._lock:
            tele = self._calls.setdefault(call_id, CallTelemetry(call_id=call_id))
            tele.bandwidth[participant_id] = {**estimate, "ts": time.time()}
        await self._fanout("bandwidth", {
            "call_id": call_id, "participant_id": participant_id, "estimate": estimate,
        })

    async def ingest_event(
        self, call_id: str, event_type: str, payload: dict[str, Any],
    ) -> None:
        """
        Append a discrete event (preset/codec/fec/simulcast/quality) to the
        per-call event log.
        """
        async with self._lock:
            tele = self._calls.setdefault(call_id, CallTelemetry(call_id=call_id))
            entry = {"ts": time.time(), "type": event_type, **payload}
            tele.events.append(entry)
        await self._fanout(event_type, {"call_id": call_id, **payload})

    # Convenience aliases that read more naturally at call-sites.
    async def record_preset_switch(self, call_id: str, participant_id: str,
                                   from_preset: str, to_preset: str, reason: str = "") -> None:
        await self.ingest_event(call_id, "preset_switch", {
            "participant_id": participant_id, "from": from_preset,
            "to": to_preset, "reason": reason,
        })

    async def record_codec_switch(self, call_id: str, participant_id: str,
                                  kind: str, from_codec: str, to_codec: str,
                                  reason: str = "") -> None:
        await self.ingest_event(call_id, "codec_switch", {
            "participant_id": participant_id, "kind": kind,
            "from": from_codec, "to": to_codec, "reason": reason,
        })

    async def record_quality_event(self, call_id: str, participant_id: str,
                                   severity: str, message: str,
                                   details: dict | None = None) -> None:
        await self.ingest_event(call_id, "quality_event", {
            "participant_id": participant_id, "severity": severity,
            "message": message, "details": details or {},
        })

    # ── Read API ───────────────────────────────────────────────────────

    def has_call(self, call_id: str) -> bool:
        return call_id in self._calls

    def snapshot(self, call_id: str) -> dict[str, Any]:
        """Full dump for the call detail page."""
        tele = self._calls.get(call_id)
        if not tele:
            return {"call_id": call_id, "streams": {}, "rtcp": {}, "events": []}

        out_streams: dict[str, list[dict]] = {}
        latest_per_stream: dict[str, dict] = {}
        for (pid, stream_id), buf in tele.streams.items():
            key = f"{pid}::{stream_id}"
            out_streams[key] = [s.to_dict() for s in buf]
            if buf:
                latest_per_stream[key] = buf[-1].to_dict()

        return {
            "call_id": call_id,
            "started_at": tele.started_at,
            "streams": out_streams,
            "latest": latest_per_stream,
            "rtcp": tele.rtcp_reports,
            "jitter_buffer": tele.jitter_buffer,
            "bandwidth": tele.bandwidth,
            "events": list(tele.events),
        }

    def latest_per_participant(self, call_id: str) -> dict[str, dict]:
        """
        Compress the rolling buffer to one row per participant — the
        WebSocket fan-out uses this so we don't ship 5 minutes of history
        on every tick.
        """
        tele = self._calls.get(call_id)
        if not tele:
            return {}
        out: dict[str, dict] = {}
        for (pid, stream_id), buf in tele.streams.items():
            if not buf:
                continue
            last = buf[-1].to_dict()
            slot = out.setdefault(pid, {"streams": {}, "mos_avg": 0.0})
            slot["streams"][stream_id] = last
        for pid, slot in out.items():
            mos_vals = [s["mos"] for s in slot["streams"].values()]
            slot["mos_avg"] = (sum(mos_vals) / len(mos_vals)) if mos_vals else 0.0
        return out

    def aggregate_summary(self) -> dict[str, Any]:
        """
        Cluster-wide aggregates for the /qos/summary endpoint.
        """
        total_calls = len(self._calls)
        total_streams = 0
        mos_samples: list[float] = []
        loss_samples: list[float] = []
        jitter_samples: list[float] = []
        rtt_samples: list[float] = []
        bitrate_samples: list[float] = []

        for tele in self._calls.values():
            for buf in tele.streams.values():
                total_streams += 1
                if not buf:
                    continue
                last = buf[-1]
                mos_samples.append(last.mos)
                loss_samples.append(last.packet_loss_pct)
                jitter_samples.append(last.jitter_ms)
                rtt_samples.append(last.rtt_ms)
                bitrate_samples.append(last.bitrate_kbps)

        def _avg(xs: Iterable[float]) -> float:
            xs = list(xs)
            return sum(xs) / len(xs) if xs else 0.0

        def _p(xs: list[float], pct: float) -> float:
            if not xs:
                return 0.0
            xs = sorted(xs)
            k = max(0, min(len(xs) - 1, int(math.ceil(pct / 100.0 * len(xs))) - 1))
            return xs[k]

        return {
            "active_calls": total_calls,
            "active_streams": total_streams,
            "mos_avg": _avg(mos_samples),
            "mos_p10": _p(mos_samples, 10),
            "mos_p50": _p(mos_samples, 50),
            "loss_avg_pct": _avg(loss_samples),
            "loss_p95_pct": _p(loss_samples, 95),
            "jitter_avg_ms": _avg(jitter_samples),
            "jitter_p95_ms": _p(jitter_samples, 95),
            "rtt_avg_ms": _avg(rtt_samples),
            "rtt_p95_ms": _p(rtt_samples, 95),
            "bitrate_avg_kbps": _avg(bitrate_samples),
            "samples": len(mos_samples),
        }

    def active_call_ids(self) -> list[str]:
        return list(self._calls.keys())

    def drop_call(self, call_id: str) -> None:
        """Free memory when a call ends."""
        self._calls.pop(call_id, None)

    def reset(self) -> None:
        """Test helper — clears all state."""
        self._calls.clear()


qos_stats_collector = QoSStatsCollector()
