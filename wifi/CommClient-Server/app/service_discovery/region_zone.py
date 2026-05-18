"""Region / zone metadata + locality scoring.

A region is a coarse geographic grouping (``us-east``, ``eu-west``,
``apac``). A zone is a finer split inside the region (separate
buildings, racks, AZs). For LAN-only deployments both default to
``default`` and the scoring contribution from locality is zero.
"""

from __future__ import annotations

from app.service_discovery.discovery_config import get_config


def my_region() -> str:
    return get_config().self_region or "default"


def my_zone() -> str:
    return get_config().self_zone or "default"


def locality_bonus(record_region: str, record_zone: str,
                   *, caller_region: str | None = None,
                   caller_zone: str | None = None) -> float:
    """Return additive bonus in [0, same_region_bonus + same_zone_bonus]."""
    cfg = get_config()
    cr = caller_region or my_region()
    cz = caller_zone or my_zone()

    bonus = 0.0
    if record_region and cr and record_region == cr:
        bonus += cfg.same_region_bonus
        if record_zone and cz and record_zone == cz:
            bonus += cfg.same_zone_bonus
    return bonus


def explain(record_region: str, record_zone: str,
            *, caller_region: str | None = None,
            caller_zone: str | None = None) -> dict:
    cfg = get_config()
    cr = caller_region or my_region()
    cz = caller_zone or my_zone()
    return {
        "caller_region":   cr,
        "caller_zone":     cz,
        "record_region":   record_region,
        "record_zone":     record_zone,
        "same_region":     record_region == cr,
        "same_zone":       record_zone == cz,
        "bonus":           locality_bonus(record_region, record_zone,
                                          caller_region=cr, caller_zone=cz),
        "config":          {
            "same_region_bonus": cfg.same_region_bonus,
            "same_zone_bonus":   cfg.same_zone_bonus,
        },
    }
