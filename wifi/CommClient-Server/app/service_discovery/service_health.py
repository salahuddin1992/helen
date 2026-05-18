"""Service health — derive a 0..1 health score per record.

Combines multiple signals:

  * Liveness         (last_heartbeat within grace?)        ⇒ 0 / 1
  * Status           (HEALTHY / DEGRADED / UNHEALTHY)      ⇒ {1, 0.6, 0.2, 0}
  * Capacity headroom (more headroom → higher score)        ⇒ 0..1
  * Phi suspicion    (cross-checked against phi_accrual)    ⇒ 1 / 0
  * Trust score      (peer reputation if peer-style record) ⇒ 0..1
  * Recent failures  (path_health is_failed)                ⇒ 0 / 1

Returned as a single 0..1 score plus the per-signal breakdown for
debugging.
"""

from __future__ import annotations

from app.service_discovery.discovery_config import get_config
from app.service_discovery.service_record import ServiceRecord, ServiceStatus


_STATUS_WEIGHT = {
    ServiceStatus.HEALTHY:     1.00,
    ServiceStatus.REGISTERING: 0.70,
    ServiceStatus.DEGRADED:    0.60,
    ServiceStatus.DRAINING:    0.30,
    ServiceStatus.UNHEALTHY:   0.20,
    ServiceStatus.DEAD:        0.00,
}


def _capacity_headroom(record: ServiceRecord) -> float:
    return max(0.0, min(1.0, record.headroom_pct() / 100.0))


def _phi_score(record: ServiceRecord) -> float:
    if not record.server_id:
        return 1.0
    try:
        from app.services.phi_accrual import get_phi_registry
        phi = get_phi_registry().detector_for(record.server_id).phi()
        if phi >= 8.0:
            return 0.0
        if phi <= 0:
            return 1.0
        return max(0.0, 1.0 - (phi / 8.0))
    except Exception:
        return 0.7  # neutral


def _trust_score(record: ServiceRecord) -> float:
    if not record.server_id:
        return 0.7
    try:
        from app.services.trust_score import get_trust_db
        return float(get_trust_db().get_score(record.server_id))
    except Exception:
        return 0.5


def _path_failed(record: ServiceRecord) -> bool:
    try:
        from app.services.path_health import get_path_health
        return get_path_health().is_failed(record.host, record.port)
    except Exception:
        return False


def health_score(record: ServiceRecord) -> tuple[float, dict]:
    cfg = get_config()
    if not record.is_alive(grace_sec=cfg.heartbeat_grace_sec):
        return 0.0, {"liveness": 0.0, "rejected": "stale"}
    status = _STATUS_WEIGHT.get(record.status, 0.5)
    cap = _capacity_headroom(record)
    phi = _phi_score(record)
    trust = _trust_score(record)
    failed = 0.0 if _path_failed(record) else 1.0

    # Weighted sum (sums to 1.0).
    score = (
        0.30 * status +
        0.20 * cap +
        0.20 * phi +
        0.15 * trust +
        0.15 * failed
    )
    breakdown = {
        "liveness":  1.0,
        "status":    round(status, 3),
        "capacity":  round(cap, 3),
        "phi":       round(phi, 3),
        "trust":     round(trust, 3),
        "path_ok":   round(failed, 3),
        "score":     round(score, 4),
    }
    return score, breakdown


def is_eligible(record: ServiceRecord) -> bool:
    """Quick yes/no: above min_health_score + has remaining capacity.

    DEAD / DRAINING / UNHEALTHY are hard-rejected regardless of
    other signals so the registry's explicit status semantics aren't
    drowned out by the composite health score.
    """
    cfg = get_config()
    if record.status in (ServiceStatus.DEAD, ServiceStatus.DRAINING,
                          ServiceStatus.UNHEALTHY):
        return False
    s, _ = health_score(record)
    if s < cfg.min_health_score:
        return False
    if record.max_capacity > 0:
        if record.headroom_pct() < cfg.capacity_floor_pct:
            return False
    return True
