"""Failure detector — facade over phi_accrual + custom probe support.

Wraps ``services.phi_accrual`` and exposes a stable resilience-flavoured
API. Custom synchronous probes can be registered for non-peer
resources (DB, disk, external service).
"""

from __future__ import annotations

import threading
from typing import Callable

from app.resilience.resilience_config import get_config
from app.resilience.resilience_events import emit


# Probe signature: () → (alive: bool, detail: str)
ProbeFn = Callable[[], tuple[bool, str]]


class FailureDetector:
    _singleton: "FailureDetector | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._probes: dict[str, ProbeFn] = {}
        self._last_state: dict[str, bool] = {}

    @classmethod
    def instance(cls) -> "FailureDetector":
        if cls._singleton is None:
            cls._singleton = FailureDetector()
        return cls._singleton

    # ── Peer-level (phi accrual passthrough) ───────────────

    def is_peer_alive(self, peer_id: str) -> bool:
        cfg = get_config()
        try:
            from app.services.phi_accrual import get_phi_registry
            return get_phi_registry().is_available(
                peer_id, threshold=cfg.phi_threshold,
            )
        except Exception:
            return True  # err on the side of trying

    def peer_phi(self, peer_id: str) -> float:
        try:
            from app.services.phi_accrual import get_phi_registry
            return get_phi_registry().detector_for(peer_id).phi()
        except Exception:
            return 0.0

    def evict_peer(self, peer_id: str) -> None:
        try:
            from app.services.phi_accrual import get_phi_registry
            get_phi_registry().evict(peer_id)
        except Exception:
            pass

    # ── Custom probes ──────────────────────────────────────

    def register_probe(self, name: str, probe: ProbeFn) -> None:
        with self._lock:
            self._probes[name] = probe

    def unregister_probe(self, name: str) -> None:
        with self._lock:
            self._probes.pop(name, None)
            self._last_state.pop(name, None)

    def run_probes(self) -> dict:
        with self._lock:
            probes = dict(self._probes)
        out: dict[str, dict] = {}
        for name, fn in probes.items():
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, f"raised:{e}"
            with self._lock:
                prev = self._last_state.get(name)
                self._last_state[name] = ok
            out[name] = {"ok": bool(ok), "detail": detail}
            if prev is not None and prev != ok:
                event = "probe.cleared" if ok else "probe.failing"
                emit(event, {"name": name, "detail": detail})
        return out

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phi_threshold": get_config().phi_threshold,
                "probes":        sorted(self._probes.keys()),
                "last_state":    dict(self._last_state),
            }


def get_failure_detector() -> FailureDetector:
    return FailureDetector.instance()
