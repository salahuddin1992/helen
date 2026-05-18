"""
Adaptive timeouts — RTT-aware per-peer deadlines.

Hard-coded ``timeout=10.0`` is a tradeoff between two failure modes:

  * Too low: a request to a slow-but-reachable peer fails spuriously.
  * Too high: a request to a dead peer holds a worker for 10 seconds.

The right answer depends on the *peer*: a same-rack peer should never
take more than 50ms; a cross-bridge peer might legitimately take 200ms.
This module derives a per-peer timeout from the live RTT distribution,
using TCP's RTO formula (RFC 6298) as a starting point:

    SRTT  = (1 - α) × SRTT  + α × R          (smoothed RTT)
    RTTVAR = (1 - β) × RTTVAR + β × |SRTT - R|
    RTO   = SRTT + max(G, K × RTTVAR)

with α=1/8, β=1/4, K=4, G=200ms (granularity).

We add three Helen-specific tweaks:

  * **Floor** — RTO never below ``MIN_TIMEOUT_SEC`` (1.0s) so we don't
    fail under transient jitter.
  * **Ceiling** — RTO never above ``MAX_TIMEOUT_SEC`` (15.0s) so dead
    peers don't hold workers indefinitely.
  * **Fallback** — peers with no samples get ``DEFAULT_TIMEOUT_SEC``.

Source samples come from ``path_health.record_success`` — every relay
or probe that already records latency for path scoring also feeds
this estimator, so no extra probes are needed.
"""

from __future__ import annotations

import threading
from typing import Optional


# RFC 6298 constants (with our floor/ceiling tweaks).
ALPHA = 1.0 / 8.0
BETA  = 1.0 / 4.0
K     = 4.0
GRANULARITY_SEC = 0.2

MIN_TIMEOUT_SEC     = 1.0
MAX_TIMEOUT_SEC     = 15.0
DEFAULT_TIMEOUT_SEC = 5.0


class _PeerEstimator:
    __slots__ = ("srtt", "rttvar", "samples")

    def __init__(self) -> None:
        self.srtt: float = 0.0
        self.rttvar: float = 0.0
        self.samples: int = 0


class AdaptiveTimeoutTracker:
    _singleton: "AdaptiveTimeoutTracker | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._peers: dict[str, _PeerEstimator] = {}

    @classmethod
    def instance(cls) -> "AdaptiveTimeoutTracker":
        if cls._singleton is None:
            cls._singleton = AdaptiveTimeoutTracker()
        return cls._singleton

    @staticmethod
    def _key(host: str, port: int) -> str:
        return f"{host}:{int(port)}"

    def record_rtt(self, host: str, port: int, rtt_sec: float) -> None:
        if rtt_sec <= 0:
            return
        k = self._key(host, port)
        with self._lock:
            est = self._peers.setdefault(k, _PeerEstimator())
            if est.samples == 0:
                # First sample: SRTT = R, RTTVAR = R/2 (RFC 6298).
                est.srtt = rtt_sec
                est.rttvar = rtt_sec / 2.0
            else:
                est.rttvar = (1 - BETA) * est.rttvar + BETA * abs(est.srtt - rtt_sec)
                est.srtt   = (1 - ALPHA) * est.srtt   + ALPHA * rtt_sec
            est.samples += 1

    def timeout_for(self, host: str, port: int) -> float:
        with self._lock:
            est = self._peers.get(self._key(host, port))
            if est is None or est.samples == 0:
                return DEFAULT_TIMEOUT_SEC
            rto = est.srtt + max(GRANULARITY_SEC, K * est.rttvar)
        return max(MIN_TIMEOUT_SEC, min(MAX_TIMEOUT_SEC, rto))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "config": {
                    "alpha": ALPHA, "beta": BETA, "k": K,
                    "granularity_sec": GRANULARITY_SEC,
                    "min_sec": MIN_TIMEOUT_SEC,
                    "max_sec": MAX_TIMEOUT_SEC,
                    "default_sec": DEFAULT_TIMEOUT_SEC,
                },
                "peers": [
                    {
                        "key":     k,
                        "srtt_ms":   round(est.srtt   * 1000, 1),
                        "rttvar_ms": round(est.rttvar * 1000, 1),
                        "rto_sec":   round(self.timeout_for(*k.rsplit(":", 1)), 3)
                            if False else round(
                                est.srtt + max(GRANULARITY_SEC, K * est.rttvar), 3
                            ),
                        "samples": est.samples,
                    }
                    for k, est in sorted(self._peers.items())
                ],
            }


def get_adaptive_timeout() -> AdaptiveTimeoutTracker:
    return AdaptiveTimeoutTracker.instance()


# ── Convenience wrappers used by relay / probes ──────────────────


def timeout_for_peer(host: str, port: int) -> float:
    return get_adaptive_timeout().timeout_for(host, port)


def record_rtt(host: str, port: int, rtt_sec: float) -> None:
    get_adaptive_timeout().record_rtt(host, port, rtt_sec)
