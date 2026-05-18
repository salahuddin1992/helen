"""
MOS (Mean Opinion Score) calculator — ITU-T G.107 E-Model.

The E-Model expresses subjective voice quality on a 1–5 MOS scale by way of
an intermediate **R-factor** (0–100):

    R = 93.2 - Id - Ie_eff - A

Where
  Id      — Delay impairment (mouth-to-ear latency)
  Ie_eff  — Equipment-impairment (codec + packet-loss-induced impairment)
  A       — Advantage factor (mobility convenience; we keep at 0 for desk
            UC traffic; can be raised to 5/10/20 for cellular/sat scenarios).

References
----------
* ITU-T G.107 (06/2015) — The E-model: a computational model for use in
  transmission planning.
* RFC 3611 §4.7 — RTCP XR Voice-IP metrics; same constants.
* Bellcore TM-Hu-001057 — Codec-specific Ie/Bpl tables (where opus/g722
  values come from since G.107 itself doesn't enumerate them).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Codec impairment table ───────────────────────────────────────────────
# Ie  — equipment impairment under zero packet loss
# Bpl — packet-loss robustness factor (higher = more PLC-resistant)
#
# Values consolidated from ITU-T G.113 Appendix I and codec vendor
# whitepapers. Keys are *lower-case* to match WebRTC RTCRtpCodecParameters.
_CODEC_IMPAIRMENT: dict[str, tuple[float, float]] = {
    # Codec   Ie     Bpl
    "opus":  (11.0, 25.0),
    "pcma":  (8.0,  24.0),   # G.711 A-law
    "pcmu":  (8.0,  24.0),   # G.711 µ-law
    "g711":  (8.0,  24.0),   # generic alias
    "g722":  (13.0, 24.0),
    "g729":  (11.0, 19.0),
    "speex": (15.0, 20.0),
    "aac":   (10.0, 22.0),
    "amrwb": (12.0, 22.0),
    "amrnb": (15.0, 19.0),
    "ilbc":  (16.0, 19.0),
    "isac":  (12.0, 21.0),
}

# Codec used when caller doesn't tell us. Opus matches the WebRTC default.
_FALLBACK_CODEC = "opus"


@dataclass(frozen=True)
class MOSResult:
    """Container for a full MOS computation — keeps everything together
    so callers can show breakdowns in the UI."""

    mos: float
    r_factor: float
    id_impairment: float
    ie_eff_impairment: float
    codec: str
    inputs: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "mos": round(self.mos, 3),
            "r_factor": round(self.r_factor, 2),
            "id": round(self.id_impairment, 2),
            "ie_eff": round(self.ie_eff_impairment, 2),
            "codec": self.codec,
            "inputs": self.inputs,
            "quality_label": MOSCalculator.quality_label(self.mos),
        }


class MOSCalculator:
    """E-Model MOS calculator. Stateless — singleton for ergonomics."""

    # Classification used by the dashboard.
    QUALITY_BANDS: tuple[tuple[float, str], ...] = (
        (4.34, "excellent"),
        (4.03, "good"),
        (3.60, "fair"),
        (3.10, "poor"),
        (0.00, "bad"),
    )

    # ── Codec lookup ────────────────────────────────────────────────────

    @staticmethod
    def codec_impairment(codec: str | None) -> tuple[float, float]:
        """Return ``(Ie, Bpl)`` for ``codec``. Falls back to opus on miss."""
        if not codec:
            return _CODEC_IMPAIRMENT[_FALLBACK_CODEC]
        key = codec.strip().lower()
        # Strip clock-rate suffix like "opus/48000"
        if "/" in key:
            key = key.split("/", 1)[0]
        return _CODEC_IMPAIRMENT.get(key, _CODEC_IMPAIRMENT[_FALLBACK_CODEC])

    @staticmethod
    def supported_codecs() -> tuple[str, ...]:
        return tuple(sorted(_CODEC_IMPAIRMENT.keys()))

    # ── Core E-Model math ───────────────────────────────────────────────

    @staticmethod
    def _heaviside(x: float) -> float:
        return 1.0 if x > 0 else 0.0

    @staticmethod
    def compute_id(mouth_to_ear_latency_ms: float) -> float:
        """
        Delay impairment.

            Id = 0.024 * d + 0.11 * (d - 177.3) * H(d - 177.3)

        ``H`` is the Heaviside step — the second term only kicks in past
        the 177.3 ms threshold where conversational interactivity starts
        to suffer.
        """
        d = max(0.0, float(mouth_to_ear_latency_ms))
        return 0.024 * d + 0.11 * (d - 177.3) * MOSCalculator._heaviside(d - 177.3)

    @staticmethod
    def compute_ie_eff(packet_loss_pct: float, codec: str | None) -> float:
        """
        Effective equipment impairment with packet loss.

            Ie_eff = Ie + (95 - Ie) * Ppl / (Ppl + Bpl)
        """
        ie, bpl = MOSCalculator.codec_impairment(codec)
        ppl = max(0.0, float(packet_loss_pct))
        # Avoid divide-by-zero when both Ppl and Bpl are 0 (shouldn't happen
        # in practice because Bpl > 0 for every real codec, but be paranoid).
        denom = ppl + bpl
        if denom <= 0:
            return ie
        return ie + (95.0 - ie) * ppl / denom

    @staticmethod
    def compute_r_factor(
        mouth_to_ear_latency_ms: float,
        packet_loss_pct: float,
        codec: str | None = None,
        advantage_factor: float = 0.0,
    ) -> float:
        """
        Composite R-factor:

            R = 93.2 - Id - Ie_eff - A

        Clipped to ``[0, 100]`` because the MOS curve below assumes that
        domain.
        """
        id_imp = MOSCalculator.compute_id(mouth_to_ear_latency_ms)
        ie_eff = MOSCalculator.compute_ie_eff(packet_loss_pct, codec)
        r = 93.2 - id_imp - ie_eff - max(0.0, float(advantage_factor))
        return max(0.0, min(100.0, r))

    @staticmethod
    def mos_from_r(r: float) -> float:
        """
        ITU-T G.107 R → MOS conversion.

            MOS = 1                           if R <= 0
                = 4.5                          if R >= 100
                = 1 + 0.035*R + 7e-6 * R*(R-60)*(100-R)
        """
        if r <= 0:
            return 1.0
        if r >= 100:
            return 4.5
        mos = 1.0 + 0.035 * r + 7e-6 * r * (r - 60.0) * (100.0 - r)
        # Numeric guard rails — the curve technically peaks just under 4.5
        return max(1.0, min(4.5, mos))

    # ── Orchestration ───────────────────────────────────────────────────

    @staticmethod
    def compute_mos(
        jitter_ms: float | None,
        loss_pct: float | None,
        rtt_ms: float | None,
        codec: str | None = None,
        advantage_factor: float = 0.0,
    ) -> MOSResult:
        """
        High-level orchestration that the dashboard calls.

        Mouth-to-ear latency is estimated from one-way delay (≈ ``rtt/2``)
        plus jitter-buffer headroom (≈ ``2 * jitter`` — a conservative
        approximation of an adaptive de-jitter buffer's target depth)
        plus 20 ms of fixed encoder/decoder + network-stack overhead.
        """
        jitter = max(0.0, float(jitter_ms or 0.0))
        loss = max(0.0, float(loss_pct or 0.0))
        rtt = max(0.0, float(rtt_ms or 0.0))

        m2e = (rtt / 2.0) + (2.0 * jitter) + 20.0

        r = MOSCalculator.compute_r_factor(
            m2e, loss, codec=codec, advantage_factor=advantage_factor,
        )
        mos = MOSCalculator.mos_from_r(r)

        return MOSResult(
            mos=mos,
            r_factor=r,
            id_impairment=MOSCalculator.compute_id(m2e),
            ie_eff_impairment=MOSCalculator.compute_ie_eff(loss, codec),
            codec=(codec or _FALLBACK_CODEC).lower(),
            inputs={
                "jitter_ms": jitter,
                "loss_pct": loss,
                "rtt_ms": rtt,
                "mouth_to_ear_ms": m2e,
                "advantage_factor": advantage_factor,
            },
        )

    @staticmethod
    def quality_label(mos: float) -> str:
        """Map a MOS value to one of excellent/good/fair/poor/bad."""
        for threshold, label in MOSCalculator.QUALITY_BANDS:
            if mos >= threshold:
                return label
        return "bad"

    @staticmethod
    def aggregate_mos(samples: Iterable[float]) -> float | None:
        """
        Time-averaged MOS — used for the call detail page where each
        stream has a per-second history.
        """
        vals = [s for s in samples if s is not None and not math.isnan(s)]
        if not vals:
            return None
        return sum(vals) / len(vals)


# Singleton — stateless but exported for parity with the other services.
qos_mos_calculator = MOSCalculator()
