"""Service scoring — composite weight to pick the best endpoint.

Weighted sum of:

    health        (0..1)  weight 0.30
    latency       (0..1)  weight 0.25
    capacity      (0..1)  weight 0.20  (headroom %)
    locality      (0..1)  weight 0.15  (region/zone bonus, normalised)
    advertised    (0..1)  weight 0.10  (capabilities match)

Scoring is *deterministic* given the same inputs so two clients in
the same region pick the same primary, which is the foundation of
session affinity.
"""

from __future__ import annotations

from app.service_discovery.discovery_config import get_config
from app.service_discovery.latency_probe import latency_score
from app.service_discovery.region_zone import locality_bonus, my_region, my_zone
from app.service_discovery.service_health import health_score
from app.service_discovery.service_record import ServiceRecord


W_HEALTH    = 0.30
W_LATENCY   = 0.25
W_CAPACITY  = 0.20
W_LOCALITY  = 0.15
W_ADVERTISE = 0.10


def _capacity_score(record: ServiceRecord) -> float:
    cfg = get_config()
    if record.max_capacity <= 0:
        return 0.7
    headroom = record.headroom_pct()
    if headroom < cfg.capacity_floor_pct:
        return 0.0
    return max(0.0, min(1.0, headroom / 100.0))


def _normalised_locality(record: ServiceRecord,
                         caller_region: str,
                         caller_zone: str) -> float:
    cfg = get_config()
    bonus = locality_bonus(record.region, record.zone,
                           caller_region=caller_region,
                           caller_zone=caller_zone)
    max_bonus = cfg.same_region_bonus + cfg.same_zone_bonus
    if max_bonus <= 0:
        return 0.0
    return max(0.0, min(1.0, bonus / max_bonus))


def _advertise_match(record: ServiceRecord,
                     required_caps: dict | None) -> float:
    if not required_caps:
        return 0.7
    caps = record.capabilities or {}
    have = sum(1 for k, v in required_caps.items() if caps.get(k) == v)
    total = max(1, len(required_caps))
    return have / total


def score(record: ServiceRecord, *,
          caller_region: str | None = None,
          caller_zone: str | None = None,
          required_caps: dict | None = None) -> tuple[float, dict]:
    """Compute composite score (0..1+) plus a breakdown for tracing."""
    cr = caller_region or my_region()
    cz = caller_zone or my_zone()
    h, h_break = health_score(record)
    l = latency_score(record)
    c = _capacity_score(record)
    g = _normalised_locality(record, cr, cz)
    a = _advertise_match(record, required_caps)
    final = (
        W_HEALTH * h +
        W_LATENCY * l +
        W_CAPACITY * c +
        W_LOCALITY * g +
        W_ADVERTISE * a
    )
    breakdown = {
        "health":     round(h, 4),
        "latency":    round(l, 4),
        "capacity":   round(c, 4),
        "locality":   round(g, 4),
        "advertise":  round(a, 4),
        "final":      round(final, 4),
        "health_breakdown": h_break,
    }
    return final, breakdown
