"""Alert manager — threshold-based event firing.

Each alert rule is a callable that returns ``(firing: bool, detail:
str)``. The manager runs every rule on a schedule and emits
``alert.fired`` / ``alert.cleared`` events on the monitoring bus.

State is kept per rule so we don't re-fire the same alert every
cycle — only on transitions.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core.logging import get_logger
from app.monitoring.monitoring_config import get_config
from app.monitoring.monitoring_events import emit
from app.monitoring.monitoring_exceptions import AlertConfigError

logger = get_logger(__name__)


# Rule signature: () → (firing, detail).
RuleFn = Callable[[], tuple[bool, str]]


@dataclass
class AlertState:
    name:     str
    firing:   bool = False
    since:    float = 0.0
    last_check_at: float = 0.0
    detail:   str = ""
    history:  list[tuple[float, bool, str]] = field(default_factory=list)


# ── Built-in rules ──────────────────────────────────────────────


def _rule_partition_minority() -> tuple[bool, str]:
    try:
        from app.services.partition_detector import get_partition_state
        snap = get_partition_state().snapshot()
        if not snap.get("is_majority", True):
            return True, f"only {snap.get('fresh_count', 0)} of {snap.get('high_water', 1)} visible"
        return False, "majority"
    except Exception as e:
        return False, f"partition_unavailable:{e}"


def _rule_backpressure_rejected() -> tuple[bool, str]:
    try:
        from app.services.backpressure import get_backpressure
        snap = get_backpressure().snapshot()
        if snap.get("level") == "rejected":
            return True, f"saturation={snap.get('saturation')}"
        return False, snap.get("level", "normal")
    except Exception as e:
        return False, f"backpressure_unavailable:{e}"


def _rule_audit_chain_broken() -> tuple[bool, str]:
    try:
        from app.services.audit_replication import get_audit_replicator
        v = get_audit_replicator().verify_chain(max_entries=100)
        if not v.get("ok", True):
            return True, f"broken_at={v.get('broken_at')}"
        return False, "ok"
    except Exception as e:
        return False, f"audit_unavailable:{e}"


def _rule_routing_strategy_unhealthy() -> tuple[bool, str]:
    try:
        from app.routing_strategy import get_strategy_manager
        snap = get_strategy_manager().snapshot()
        m = snap.get("metrics", {}).get("counters", {})
        total = m.get("decisions_total", 0)
        unresolved = m.get("decisions_unresolved", 0)
        if total >= 20 and unresolved / max(1, total) > 0.5:
            return True, f"unresolved_rate={unresolved}/{total}"
        return False, "ok"
    except Exception as e:
        return False, f"strategy_unavailable:{e}"


def _rule_disk_full() -> tuple[bool, str]:
    try:
        import shutil
        usage = shutil.disk_usage("/")
        pct_used = (usage.used / usage.total) * 100.0
        if pct_used >= 90.0:
            return True, f"disk={pct_used:.1f}% used"
        return False, f"disk={pct_used:.1f}%"
    except Exception as e:
        return False, f"disk_unavailable:{e}"


def _rule_backup_overdue() -> tuple[bool, str]:
    try:
        from app.services.backup_scheduler import get_backup_scheduler
        snap = get_backup_scheduler().snapshot()
        last_ok = snap.get("last_success_at") or 0
        if last_ok and (time.time() - float(last_ok)) > 24 * 3600:
            hours = (time.time() - float(last_ok)) / 3600.0
            return True, f"last_backup_{hours:.1f}h_ago"
        return False, "ok" if last_ok else "never_run"
    except Exception as e:
        return False, f"backup_unavailable:{e}"


def _rule_event_loop_lagging() -> tuple[bool, str]:
    try:
        from app.services.load_monitor import get_load_monitor
        m = get_load_monitor()
        if m is None:
            return False, "monitor_off"
        last = m.last()
        if last is None:
            return False, "no_sample"
        lag_ms = float(getattr(last, "event_loop_lag_ms", 0.0) or 0.0)
        if lag_ms >= 250.0:
            return True, f"event_loop_lag={lag_ms:.0f}ms"
        return False, f"lag={lag_ms:.0f}ms"
    except Exception as e:
        return False, f"loadmon_unavailable:{e}"


_DEFAULT_RULES: dict[str, RuleFn] = {
    "partition_minority":         _rule_partition_minority,
    "backpressure_rejected":      _rule_backpressure_rejected,
    "audit_chain_broken":         _rule_audit_chain_broken,
    "routing_strategy_unhealthy": _rule_routing_strategy_unhealthy,
    "disk_full":                  _rule_disk_full,
    "backup_overdue":             _rule_backup_overdue,
    "event_loop_lagging":         _rule_event_loop_lagging,
}


# ── Manager singleton ──────────────────────────────────────────


class AlertManager:
    _singleton: "AlertManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rules: dict[str, RuleFn] = dict(_DEFAULT_RULES)
        self._state: dict[str, AlertState] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "AlertManager":
        if cls._singleton is None:
            cls._singleton = AlertManager()
        return cls._singleton

    # ── Rule registration ───────────────────────────────────

    def register_rule(self, name: str, fn: RuleFn) -> None:
        if not callable(fn):
            raise AlertConfigError(f"rule {name!r} not callable")
        with self._lock:
            self._rules[name] = fn

    def unregister_rule(self, name: str) -> None:
        with self._lock:
            self._rules.pop(name, None)
            self._state.pop(name, None)

    # ── Run ─────────────────────────────────────────────────

    def check_once(self) -> dict:
        cfg = get_config()
        if not cfg.enable_alerts:
            return {"skipped": "alerts_disabled"}
        with self._lock:
            rules = dict(self._rules)
        results: dict[str, dict] = {}
        for name, fn in rules.items():
            try:
                firing, detail = fn()
            except Exception as e:
                firing, detail = False, f"raised:{e}"
            with self._lock:
                state = self._state.get(name) or AlertState(name=name)
                changed = state.firing != firing
                if changed:
                    state.since = time.time()
                state.firing = bool(firing)
                state.last_check_at = time.time()
                state.detail = detail
                state.history.append((time.time(), state.firing, detail))
                state.history = state.history[-50:]
                self._state[name] = state
            results[name] = {
                "firing": state.firing,
                "detail": detail,
                "since":  state.since,
                "changed": changed,
            }
            if changed:
                event = "alert.fired" if firing else "alert.cleared"
                emit(event, {"name": name, "detail": detail})
        return {
            "checked": len(results),
            "firing":  sum(1 for r in results.values() if r["firing"]),
            "results": results,
        }

    def state(self, name: str) -> Optional[dict]:
        with self._lock:
            s = self._state.get(name)
        if s is None:
            return None
        return {
            "name":          s.name,
            "firing":        s.firing,
            "since":         s.since,
            "last_check_at": s.last_check_at,
            "detail":        s.detail,
            "history":       list(s.history)[-20:],
        }

    def all_states(self) -> dict:
        with self._lock:
            names = list(self._state.keys())
        return {n: self.state(n) for n in names}

    # ── Background loop ─────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info(
            "monitoring_alerts_started",
            interval_sec=cfg.alert_check_interval_sec,
        )
        try:
            while self._running:
                try:
                    self.check_once()
                except Exception as e:
                    logger.warning("monitoring_alerts_failed", error=str(e))
                await asyncio.sleep(cfg.alert_check_interval_sec)
        finally:
            logger.info("monitoring_alerts_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="monitoring-alerts",
            )
        except RuntimeError:
            logger.warning("monitoring_alerts_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_alert_manager() -> AlertManager:
    return AlertManager.instance()
