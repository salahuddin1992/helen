"""Service lookup — public "give me an endpoint" API.

Three primary functions:

  * ``find_best(service_type, ...)`` — single best endpoint.
  * ``find_top_k(service_type, k=3, ...)`` — failover chain.
  * ``find_nearest_relay()`` / ``find_nearest_signaling()`` /
    ``find_nearest_media_gateway()`` — convenience wrappers.

Selection goes through:

    registry.by_type(...)
        ↓
    filter alive + healthy + capacity > floor
        ↓
    score each survivor (service_scoring.score)
        ↓
    sort descending; tiebreak on (lower hop count, lex service_id).
"""

from __future__ import annotations

from typing import Optional

from app.service_discovery.discovery_exceptions import ServiceNotFoundError
from app.service_discovery.region_zone import my_region, my_zone
from app.service_discovery.service_health import is_eligible
from app.service_discovery.service_record import ServiceRecord, ServiceType
from app.service_discovery.service_registry import get_registry
from app.service_discovery.service_scoring import score


def _eligible_candidates(
    service_type: ServiceType,
    *,
    region: Optional[str] = None,
    zone: Optional[str] = None,
    cluster_id: Optional[str] = None,
    required_tags: Optional[set[str]] = None,
) -> list[ServiceRecord]:
    candidates = get_registry().by_type(service_type)
    out: list[ServiceRecord] = []
    for r in candidates:
        if not is_eligible(r):
            continue
        if region is not None and r.region != region:
            continue
        if zone is not None and r.zone != zone:
            continue
        if cluster_id is not None and r.cluster_id != cluster_id:
            continue
        if required_tags and not required_tags.issubset(r.tags):
            continue
        out.append(r)
    return out


def find_top_k(
    service_type: ServiceType,
    *,
    k: int = 3,
    region: Optional[str] = None,
    zone: Optional[str] = None,
    cluster_id: Optional[str] = None,
    required_caps: Optional[dict] = None,
    required_tags: Optional[set[str]] = None,
    caller_region: Optional[str] = None,
    caller_zone: Optional[str] = None,
) -> list[tuple[ServiceRecord, float, dict]]:
    """Return top-K eligible records, scored."""
    cands = _eligible_candidates(
        service_type,
        region=region, zone=zone, cluster_id=cluster_id,
        required_tags=required_tags,
    )
    cr = caller_region or my_region()
    cz = caller_zone or my_zone()
    scored: list[tuple[ServiceRecord, float, dict]] = []
    for r in cands:
        s, b = score(r,
                     caller_region=cr, caller_zone=cz,
                     required_caps=required_caps)
        scored.append((r, s, b))
    scored.sort(
        key=lambda x: (-x[1], x[0].service_id),
    )
    return scored[: max(1, int(k))]


def find_best(
    service_type: ServiceType,
    *,
    region: Optional[str] = None,
    zone: Optional[str] = None,
    cluster_id: Optional[str] = None,
    required_caps: Optional[dict] = None,
    required_tags: Optional[set[str]] = None,
    caller_region: Optional[str] = None,
    caller_zone: Optional[str] = None,
) -> tuple[ServiceRecord, float, dict]:
    """Single best record. Raises ServiceNotFoundError if none."""
    top = find_top_k(
        service_type, k=1,
        region=region, zone=zone, cluster_id=cluster_id,
        required_caps=required_caps,
        required_tags=required_tags,
        caller_region=caller_region, caller_zone=caller_zone,
    )
    if not top:
        raise ServiceNotFoundError(
            f"no eligible {service_type.value} service"
        )
    return top[0]


# ── Convenience wrappers for hot paths ──────────────────────────


def find_nearest_relay(**kwargs) -> ServiceRecord:
    return find_best(ServiceType.RELAY, **kwargs)[0]


def find_nearest_signaling(**kwargs) -> ServiceRecord:
    return find_best(ServiceType.SIGNALING, **kwargs)[0]


def find_nearest_media_gateway(**kwargs) -> ServiceRecord:
    return find_best(ServiceType.MEDIA_GATEWAY, **kwargs)[0]


def find_nearest_bridge(**kwargs) -> ServiceRecord:
    return find_best(ServiceType.BRIDGE, **kwargs)[0]


def find_nearest_rendezvous(**kwargs) -> ServiceRecord:
    return find_best(ServiceType.RENDEZVOUS, **kwargs)[0]


def find_failover_chain(
    service_type: ServiceType,
    *,
    k: int = 3,
    **kwargs,
) -> list[ServiceRecord]:
    """Return just the records (no scores) for use as a failover chain."""
    return [r for r, _, _ in find_top_k(service_type, k=k, **kwargs)]
