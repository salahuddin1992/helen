"""Failure detector — facade over services.phi_accrual.

The phi-accrual primitive lives in services/; this file exposes a
distributed-system-flavoured API:

  * ``is_alive(node_id)``     → healthy?
  * ``suspect_level(node_id)`` → φ score
  * ``snapshot()``            → all peers' detector states
"""

from __future__ import annotations

from app.distributed_system.distributed_exceptions import FailureDetectorError


def is_alive(node_id: str, threshold: float = 8.0) -> bool:
    try:
        from app.services.phi_accrual import get_phi_registry
        return get_phi_registry().is_available(node_id, threshold=threshold)
    except Exception as e:
        raise FailureDetectorError(str(e))


def suspect_level(node_id: str) -> float:
    try:
        from app.services.phi_accrual import get_phi_registry
        return get_phi_registry().detector_for(node_id).phi()
    except Exception:
        return 0.0


def snapshot() -> dict:
    try:
        from app.services.phi_accrual import get_phi_registry
        return get_phi_registry().snapshot()
    except Exception:
        return {}


def evict(node_id: str) -> None:
    try:
        from app.services.phi_accrual import get_phi_registry
        get_phi_registry().evict(node_id)
    except Exception:
        pass
