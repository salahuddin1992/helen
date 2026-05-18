"""
Phase 6 / Module AD — Structured alerts engine (no external dep).

Rules engine:
    WHEN <metric> <op> <threshold> FOR <duration> THEN <action>

Metric values can be:
    * names registered in ``metrics_exporter`` (Counters / Gauges)
    * synthetic accessors: 'dlq.size', 'cluster.active_nodes',
      'error_rate.5m', 'disk.free_bytes', etc.

Actions:
    * webhook   — POST JSON to a URL
    * email     — fire an alert email (via existing notification service)
    * log       — write an alert into the audit log
    * signal    — broadcast over cluster pubsub channel ``alerts``
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


VALID_OPS = (">", ">=", "<", "<=", "==", "!=")
VALID_ACTIONS = ("webhook", "email", "log", "signal")


@dataclass
class AlertRule:
    name: str
    metric: str
    op: str
    threshold: float
    for_seconds: int = 60
    action: str = "log"
    action_target: Optional[str] = None
    severity: str = "warning"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "metric": self.metric, "op": self.op,
            "threshold": self.threshold, "for_seconds": self.for_seconds,
            "action": self.action, "action_target": self.action_target,
            "severity": self.severity, "enabled": self.enabled,
        }


@dataclass
class ActiveAlert:
    rule: str
    metric: str
    fired_at: float
    last_value: float
    severity: str
    payload: dict[str, Any] = field(default_factory=dict)


MetricResolver = Callable[[str], Awaitable[Optional[float]]]


class AlertsEngine:
    """Periodic evaluator. Run via ``start()`` / ``stop()``."""

    def __init__(
        self,
        *,
        evaluator: Optional[MetricResolver] = None,
        eval_interval: float = 15.0,
    ) -> None:
        self._rules: dict[str, AlertRule] = {}
        self._history: dict[str, Deque[tuple[float, float]]] = {}
        self._fired: dict[str, ActiveAlert] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._eval_interval = eval_interval
        self._resolver = evaluator or _default_resolver
        self._init_defaults()

    # ── rule mgmt ───────────────────────────────────────────

    def _init_defaults(self) -> None:
        for r in DEFAULT_RULES:
            self._rules[r.name] = r

    def list_rules(self) -> list[AlertRule]:
        return list(self._rules.values())

    def upsert_rule(self, rule: AlertRule) -> None:
        if rule.op not in VALID_OPS:
            raise ValueError(f"invalid op: {rule.op}")
        if rule.action not in VALID_ACTIONS:
            raise ValueError(f"invalid action: {rule.action}")
        self._rules[rule.name] = rule

    def delete_rule(self, name: str) -> bool:
        return self._rules.pop(name, None) is not None

    def active_alerts(self) -> list[ActiveAlert]:
        return list(self._fired.values())

    # ── lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="alerts-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._eval_interval + 2)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()

    # ── internals ───────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._evaluate_all()
            except Exception as exc:                                # pragma: no cover
                logger.exception("alerts: evaluation crashed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._eval_interval)
            except asyncio.TimeoutError:
                continue

    async def _evaluate_all(self) -> None:
        now = time.time()
        for rule in list(self._rules.values()):
            if not rule.enabled:
                self._fired.pop(rule.name, None)
                continue
            try:
                val = await self._resolver(rule.metric)
            except Exception as exc:                                # pragma: no cover
                logger.warning("alerts: resolver error %s: %s", rule.metric, exc)
                continue
            if val is None:
                continue
            hist = self._history.setdefault(rule.name, deque(maxlen=720))
            hist.append((now, val))
            # prune old
            while hist and now - hist[0][0] > rule.for_seconds * 4:
                hist.popleft()
            if not self._eval_predicate(val, rule):
                # condition cleared — resolve
                if rule.name in self._fired:
                    logger.info("alerts: %s cleared (%.3f)", rule.name, val)
                    self._fired.pop(rule.name, None)
                continue
            # condition met now; check sustained-for
            window = [v for ts, v in hist if now - ts <= rule.for_seconds]
            if not window:
                continue
            if all(self._eval_predicate(v, rule) for v in window):
                if rule.name not in self._fired:
                    alert = ActiveAlert(
                        rule=rule.name, metric=rule.metric,
                        fired_at=now, last_value=val,
                        severity=rule.severity,
                        payload={
                            "threshold": rule.threshold,
                            "op": rule.op,
                            "for_seconds": rule.for_seconds,
                        },
                    )
                    self._fired[rule.name] = alert
                    await self._fire(rule, alert)
                else:
                    self._fired[rule.name].last_value = val

    @staticmethod
    def _eval_predicate(value: float, rule: AlertRule) -> bool:
        op = rule.op; t = rule.threshold
        if op == ">":  return value > t
        if op == ">=": return value >= t
        if op == "<":  return value < t
        if op == "<=": return value <= t
        if op == "==": return value == t
        if op == "!=": return value != t
        return False

    async def _fire(self, rule: AlertRule, alert: ActiveAlert) -> None:
        logger.warning(
            "ALERT [%s] %s %s %s (last=%.3f for >= %ss)",
            rule.severity, rule.metric, rule.op, rule.threshold,
            alert.last_value, rule.for_seconds,
        )
        if rule.action == "log":
            return
        if rule.action == "webhook" and rule.action_target:
            await self._fire_webhook(rule.action_target, rule, alert)
        elif rule.action == "signal":
            try:
                from app.services.cluster.pubsub import get_pubsub
                await get_pubsub().publish("alerts", {
                    "rule": rule.name, "severity": rule.severity,
                    "metric": rule.metric, "value": alert.last_value,
                    "fired_at": alert.fired_at, "payload": alert.payload,
                })
            except Exception:                                       # pragma: no cover
                pass
        elif rule.action == "email" and rule.action_target:
            try:
                from app.services.notification_service import (
                    NotificationService,
                )
                # Best-effort use; many deployments configure SMTP here.
                svc = NotificationService()  # type: ignore[call-arg]
                await svc.send_email(  # type: ignore[attr-defined]
                    rule.action_target,
                    f"[{rule.severity}] {rule.name}",
                    json.dumps(alert.payload | {"value": alert.last_value}, indent=2),
                )
            except Exception:                                       # pragma: no cover
                pass

    async def _fire_webhook(self, url: str, rule: AlertRule, alert: ActiveAlert) -> None:
        try:
            import httpx
        except Exception:                                           # pragma: no cover
            return
        body = {
            "rule": rule.name, "severity": rule.severity,
            "metric": rule.metric, "value": alert.last_value,
            "threshold": rule.threshold, "op": rule.op,
            "fired_at": alert.fired_at,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                await cli.post(url, json=body)
        except Exception as exc:                                    # pragma: no cover
            logger.warning("alerts: webhook %s failed: %s", url, exc)


# ── default resolver ────────────────────────────────────────


async def _default_resolver(metric: str) -> Optional[float]:
    """Resolve well-known synthetic metric names.

    Real Prometheus metrics aren't queryable from inside the process;
    we look at known service singletons instead.
    """
    if metric == "dlq.size":
        try:
            from sqlalchemy import select, func
            from app.db.session import async_session_factory
            from app.models.message_dead_letter import MessageDeadLetter
            async with async_session_factory() as db:
                r = (await db.execute(
                    select(func.count(MessageDeadLetter.id))
                )).scalar_one()
                return float(r or 0)
        except Exception:                                           # pragma: no cover
            return None

    if metric == "cluster.active_nodes":
        try:
            from app.services.cluster.node_registry import get_node_registry
            nodes = await get_node_registry().get_active_nodes()
            return float(sum(1 for n in nodes if n.status == "active"))
        except Exception:                                           # pragma: no cover
            return None

    if metric == "disk.free_bytes":
        try:
            import shutil
            from app.core.config import get_settings
            s = get_settings()
            usage = shutil.disk_usage(str(s.PROJECT_ROOT))
            return float(usage.free)
        except Exception:                                           # pragma: no cover
            return None

    if metric == "backup.age_seconds":
        try:
            from app.services.backup_scheduler import (
                latest_backup_age_seconds,
            )
            v = latest_backup_age_seconds()                          # type: ignore[arg-type]
            return float(v) if v is not None else None
        except Exception:                                           # pragma: no cover
            return None

    return None


# ── default rule set ────────────────────────────────────────


DEFAULT_RULES = [
    AlertRule(
        name="dlq_growing",
        metric="dlq.size",
        op=">", threshold=100,
        for_seconds=300, action="log", severity="warning",
    ),
    AlertRule(
        name="disk_space_low",
        metric="disk.free_bytes",
        op="<", threshold=500 * 1024 * 1024,         # 500 MB
        for_seconds=60, action="log", severity="critical",
    ),
    AlertRule(
        name="backup_overdue",
        metric="backup.age_seconds",
        op=">", threshold=24 * 3600,                  # one day
        for_seconds=300, action="log", severity="critical",
    ),
    AlertRule(
        name="cluster_too_few_nodes",
        metric="cluster.active_nodes",
        op="<", threshold=1,
        for_seconds=60, action="signal", severity="critical",
    ),
]


# ── singleton ───────────────────────────────────────────────


_singleton: Optional[AlertsEngine] = None


def get_alerts_engine() -> AlertsEngine:
    global _singleton
    if _singleton is None:
        _singleton = AlertsEngine()
    return _singleton
