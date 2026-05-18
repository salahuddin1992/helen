"""
Zero-Trust — per-session risk engine.

Computes a 0..100 risk score per request/session based on:

    * device risk             (from posture)
    * location anomaly        (geo jump > 1000 km in <1 hr)
    * time anomaly            (outside user's typical hours)
    * behaviour anomaly       (sudden burst of actions)
    * threat intel            (IP in known-bad list)

Threshold-based decisions:
    ≤ 30  → allow
    30–70 → step-up auth (MFA)
    > 70  → deny
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


STEP_UP_THRESHOLD = 30
DENY_THRESHOLD = 70


@dataclass
class RiskContext:
    user_id: Optional[str] = None
    ip: str = ""
    country: str = ""
    last_country: str = ""
    last_seen_lat: float = 0.0
    last_seen_lng: float = 0.0
    current_lat: float = 0.0
    current_lng: float = 0.0
    seconds_since_last: float = 0.0
    device_risk: int = 0
    actions_per_minute: float = 0.0
    typical_hour_min: int = 6
    typical_hour_max: int = 23
    current_hour: int = 12
    on_threat_intel: bool = False


@dataclass
class RiskAssessment:
    score: int
    factors: dict[str, int]
    decision: str  # "allow" | "step_up" | "deny"


_THREAT_INTEL: set[str] = set()


def add_to_threat_intel(ip: str) -> None:
    if ip:
        _THREAT_INTEL.add(ip)


def is_known_bad(ip: str) -> bool:
    return ip in _THREAT_INTEL


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    a1, a2 = math.radians(lat1), math.radians(lat2)
    da = a2 - a1
    db = math.radians(lng2 - lng1)
    x = math.sin(da/2)**2 + math.cos(a1) * math.cos(a2) * math.sin(db/2)**2
    return 2 * R * math.asin(math.sqrt(x))


class RiskEngine:
    def assess(self, ctx: RiskContext) -> RiskAssessment:
        factors: dict[str, int] = {}

        factors["device"] = max(0, min(40, int(ctx.device_risk * 0.4)))

        # Geo anomaly: large physical jump in short time = teleport.
        if (ctx.last_seen_lat or ctx.last_seen_lng) and ctx.seconds_since_last > 0:
            km = _haversine(ctx.last_seen_lat, ctx.last_seen_lng,
                            ctx.current_lat, ctx.current_lng)
            speed_kmh = km / (ctx.seconds_since_last / 3600.0) if ctx.seconds_since_last > 0 else 0.0
            # Faster than commercial flight = impossible travel.
            if speed_kmh > 900:
                factors["geo"] = 35
            elif speed_kmh > 500:
                factors["geo"] = 20
            elif speed_kmh > 200:
                factors["geo"] = 10
            else:
                factors["geo"] = 0
        else:
            factors["geo"] = 0

        # Country flip.
        if ctx.country and ctx.last_country and ctx.country != ctx.last_country:
            factors["country_change"] = 15

        # Time anomaly.
        if ctx.current_hour < ctx.typical_hour_min or ctx.current_hour > ctx.typical_hour_max:
            factors["time"] = 10
        else:
            factors["time"] = 0

        # Behaviour anomaly — high actions per minute.
        if ctx.actions_per_minute > 100:
            factors["burst"] = 25
        elif ctx.actions_per_minute > 50:
            factors["burst"] = 12

        # Threat intel.
        if ctx.on_threat_intel or is_known_bad(ctx.ip):
            factors["threat_intel"] = 60

        score = min(100, sum(factors.values()))
        if score > DENY_THRESHOLD:
            decision = "deny"
        elif score > STEP_UP_THRESHOLD:
            decision = "step_up"
        else:
            decision = "allow"
        return RiskAssessment(score=score, factors=factors, decision=decision)


_engine: Optional[RiskEngine] = None


def get_risk_engine() -> RiskEngine:
    global _engine
    if _engine is None:
        _engine = RiskEngine()
    return _engine
