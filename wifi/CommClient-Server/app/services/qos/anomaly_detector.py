"""
QoS Anomaly Detector — threshold + temporal anomaly engine.

Reads from the in-process ``qos_stats_collector`` and surfaces structured
anomaly records to the dashboard. Each anomaly carries:

  * ``severity``     — info / warn / critical
  * ``code``         — stable machine-readable identifier (used for tests)
  * ``message``      — human-readable summary
  * ``participant``  — affected user
  * ``stream``       — affected stream key (or None for participant-wide)
  * ``recommended``  — list of suggested admin actions

Detection rules
---------------
  loss_sustained         — > 5% loss over 5+ consecutive samples.
  jitter_high            — jitter_ms > 100 over 5+ samples.
  rtt_high               — rtt_ms > 500 over 5+ samples.
  mos_low                — mos < 3.0 (any sample).
  mos_collapsed          — mos < 2.5 sustained for 10s.
  preset_flapping        — >= 3 preset switches within 60s.
  codec_flapping         — > 3 codec switches within the call.
  bandwidth_collapse     — bitrate dropped >70% from peak.
  frame_drop_storm       — > 30% frames_dropped of frames sent (video).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.services.qos.stats_collector import qos_stats_collector

logger = get_logger(__name__)


# ── Thresholds ─────────────────────────────────────────────────────────
LOSS_PCT_WARN = 5.0
JITTER_MS_WARN = 100.0
RTT_MS_WARN = 500.0
MOS_LOW = 3.0
MOS_COLLAPSED = 2.5
SUSTAINED_SAMPLES = 5            # ~5s at 1Hz
COLLAPSED_SAMPLES = 10
PRESET_FLAP_WINDOW_S = 60.0
PRESET_FLAP_COUNT = 3
CODEC_FLAP_COUNT = 3
BW_DROP_RATIO = 0.30             # current < 30% of peak
FRAME_DROP_RATIO = 0.30


@dataclass
class Anomaly:
    code: str
    severity: str
    message: str
    call_id: str
    participant_id: str | None
    stream_id: str | None
    detected_at: float = field(default_factory=time.time)
    recommended: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QoSAnomalyDetector:
    """Stateless scanner — invoked on-demand by the API layer."""

    def __init__(self) -> None:
        self._collector = qos_stats_collector

    # ── Public entry point ─────────────────────────────────────────────

    def detect(self, call_id: str) -> list[Anomaly]:
        """Return every anomaly currently present in ``call_id``."""
        snap = self._collector.snapshot(call_id)
        if not snap or not snap.get("streams"):
            return []

        anomalies: list[Anomaly] = []
        anomalies.extend(self._scan_streams(call_id, snap))
        anomalies.extend(self._scan_events(call_id, snap))
        return anomalies

    def detect_all(self) -> dict[str, list[Anomaly]]:
        """Scan every active call. Used by the global summary endpoint."""
        out: dict[str, list[Anomaly]] = {}
        for cid in self._collector.active_call_ids():
            anomalies = self.detect(cid)
            if anomalies:
                out[cid] = anomalies
        return out

    # ── Stream-level rules ─────────────────────────────────────────────

    def _scan_streams(self, call_id: str, snap: dict) -> list[Anomaly]:
        out: list[Anomaly] = []
        for key, samples in snap["streams"].items():
            if not samples:
                continue
            pid, stream_id = key.split("::", 1)
            recent = samples[-SUSTAINED_SAMPLES:]

            # Sustained packet loss
            if (
                len(recent) >= SUSTAINED_SAMPLES
                and all(s["loss_pct"] > LOSS_PCT_WARN for s in recent)
            ):
                avg = sum(s["loss_pct"] for s in recent) / len(recent)
                out.append(Anomaly(
                    code="loss_sustained",
                    severity="warn" if avg < 15 else "critical",
                    message=f"Sustained packet loss {avg:.1f}% on {stream_id}",
                    call_id=call_id, participant_id=pid, stream_id=stream_id,
                    recommended=[
                        "force_preset:low",
                        "toggle_simulcast:off",
                        "force_turn",
                    ],
                    context={"avg_loss_pct": avg},
                ))

            # High jitter
            if (
                len(recent) >= SUSTAINED_SAMPLES
                and all(s["jitter_ms"] > JITTER_MS_WARN for s in recent)
            ):
                avg = sum(s["jitter_ms"] for s in recent) / len(recent)
                out.append(Anomaly(
                    code="jitter_high",
                    severity="warn",
                    message=f"High jitter {avg:.0f}ms on {stream_id}",
                    call_id=call_id, participant_id=pid, stream_id=stream_id,
                    recommended=["force_preset:low", "force_codec:opus"],
                    context={"avg_jitter_ms": avg},
                ))

            # Round-trip time
            if (
                len(recent) >= SUSTAINED_SAMPLES
                and all(s["rtt_ms"] > RTT_MS_WARN for s in recent)
            ):
                avg = sum(s["rtt_ms"] for s in recent) / len(recent)
                out.append(Anomaly(
                    code="rtt_high",
                    severity="warn",
                    message=f"High round-trip {avg:.0f}ms on {stream_id}",
                    call_id=call_id, participant_id=pid, stream_id=stream_id,
                    recommended=["force_turn"],
                    context={"avg_rtt_ms": avg},
                ))

            # MOS collapsed
            collapsed = [s for s in samples[-COLLAPSED_SAMPLES:] if s["mos"] < MOS_COLLAPSED]
            if len(collapsed) >= COLLAPSED_SAMPLES:
                avg = sum(s["mos"] for s in collapsed) / len(collapsed)
                out.append(Anomaly(
                    code="mos_collapsed",
                    severity="critical",
                    message=f"MOS collapsed to {avg:.2f}",
                    call_id=call_id, participant_id=pid, stream_id=stream_id,
                    recommended=["force_preset:low", "force_codec:opus", "force_turn"],
                    context={"avg_mos": avg},
                ))
            elif samples[-1]["mos"] < MOS_LOW:
                out.append(Anomaly(
                    code="mos_low",
                    severity="warn",
                    message=f"MOS dipped to {samples[-1]['mos']:.2f}",
                    call_id=call_id, participant_id=pid, stream_id=stream_id,
                    recommended=["force_preset:low"],
                    context={"mos": samples[-1]["mos"]},
                ))

            # Bandwidth collapse
            peak_bw = max((s["bitrate_kbps"] for s in samples), default=0)
            if peak_bw > 0 and samples[-1]["bitrate_kbps"] < peak_bw * BW_DROP_RATIO:
                out.append(Anomaly(
                    code="bandwidth_collapse",
                    severity="warn",
                    message=(f"Bitrate fell from {peak_bw:.0f} to "
                             f"{samples[-1]['bitrate_kbps']:.0f} kbps"),
                    call_id=call_id, participant_id=pid, stream_id=stream_id,
                    recommended=["toggle_simulcast:off"],
                    context={
                        "peak_kbps": peak_bw,
                        "current_kbps": samples[-1]["bitrate_kbps"],
                    },
                ))

            # Frame drop storm (video)
            if samples[-1]["kind"] == "video":
                sent = samples[-1]["packets_sent"] or 1
                dropped = samples[-1].get("frames_dropped") or 0
                if dropped > 0 and dropped / max(sent, 1) > FRAME_DROP_RATIO:
                    out.append(Anomaly(
                        code="frame_drop_storm",
                        severity="warn",
                        message=f"Video dropping {dropped} frames",
                        call_id=call_id, participant_id=pid, stream_id=stream_id,
                        recommended=["force_preset:low", "toggle_simulcast:off"],
                        context={"frames_dropped": dropped},
                    ))
        return out

    # ── Event-level rules ──────────────────────────────────────────────

    def _scan_events(self, call_id: str, snap: dict) -> list[Anomaly]:
        out: list[Anomaly] = []
        events = snap.get("events") or []
        now = time.time()

        preset_per_pid: dict[str, list[float]] = {}
        codec_per_pid: dict[str, int] = {}
        for ev in events:
            pid = ev.get("participant_id")
            if not pid:
                continue
            if ev.get("type") == "preset_switch":
                preset_per_pid.setdefault(pid, []).append(ev["ts"])
            elif ev.get("type") == "codec_switch":
                codec_per_pid[pid] = codec_per_pid.get(pid, 0) + 1

        for pid, timestamps in preset_per_pid.items():
            recent = [t for t in timestamps if now - t <= PRESET_FLAP_WINDOW_S]
            if len(recent) >= PRESET_FLAP_COUNT:
                out.append(Anomaly(
                    code="preset_flapping",
                    severity="warn",
                    message=f"{len(recent)} preset switches in 60s",
                    call_id=call_id, participant_id=pid, stream_id=None,
                    recommended=["force_preset:low"],
                    context={"recent_switches": len(recent)},
                ))

        for pid, count in codec_per_pid.items():
            if count > CODEC_FLAP_COUNT:
                out.append(Anomaly(
                    code="codec_flapping",
                    severity="warn",
                    message=f"{count} codec switches in this call",
                    call_id=call_id, participant_id=pid, stream_id=None,
                    recommended=["force_codec:opus"],
                    context={"switches": count},
                ))

        return out


qos_anomaly_detector = QoSAnomalyDetector()
