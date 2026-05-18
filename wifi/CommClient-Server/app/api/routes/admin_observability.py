"""
Phase 6 / Module AD — Observability admin REST endpoints.

Mounted under ``/api/admin/observability``. Requires ``observability.manage``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.observability.log_shipper import get_log_shipper
from app.observability.otel_tracing import current_trace_id, status as otel_status
from app.observability.structured_alerts import (
    AlertRule,
    VALID_ACTIONS,
    VALID_OPS,
    get_alerts_engine,
)
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/observability", tags=["admin-observability"])
_PERM = "observability.manage"


class AlertRuleIn(BaseModel):
    name: str
    metric: str
    op: str = Field(..., description=">|>=|<|<=|==|!=")
    threshold: float
    for_seconds: int = 60
    action: str = "log"
    action_target: Optional[str] = None
    severity: str = "warning"
    enabled: bool = True


class AlertRuleOut(AlertRuleIn):
    pass


class ActiveAlertOut(BaseModel):
    rule: str
    metric: str
    severity: str
    last_value: float
    fired_at: float
    payload: dict[str, Any]


@router.get("/status")
async def status(
    _user: str = Depends(require_permission(_PERM)),
):
    shipper = get_log_shipper()
    return {
        "otel": otel_status(),
        "loki": {
            "enabled": shipper is not None and shipper._enabled,
            "url": getattr(shipper, "url", None),
        },
        "prometheus": {"endpoint": "/metrics"},
        "alerts": {
            "rules": len(get_alerts_engine().list_rules()),
            "active": len(get_alerts_engine().active_alerts()),
        },
    }


@router.get("/alerts/rules", response_model=list[AlertRuleOut])
async def list_alert_rules(
    _user: str = Depends(require_permission(_PERM)),
):
    return [AlertRuleOut(**r.to_dict()) for r in get_alerts_engine().list_rules()]


@router.post("/alerts/rules", response_model=AlertRuleOut)
async def upsert_alert_rule(
    body: AlertRuleIn,
    _user: str = Depends(require_permission(_PERM)),
):
    if body.op not in VALID_OPS:
        raise HTTPException(400, f"invalid op (use one of {VALID_OPS})")
    if body.action not in VALID_ACTIONS:
        raise HTTPException(400, f"invalid action (use one of {VALID_ACTIONS})")
    rule = AlertRule(**body.model_dump())
    get_alerts_engine().upsert_rule(rule)
    return AlertRuleOut(**rule.to_dict())


@router.delete("/alerts/rules/{name}")
async def delete_alert_rule(
    name: str = Path(...),
    _user: str = Depends(require_permission(_PERM)),
):
    ok = get_alerts_engine().delete_rule(name)
    if not ok:
        raise HTTPException(404, "rule not found")
    return {"status": "deleted"}


@router.get("/alerts/active", response_model=list[ActiveAlertOut])
async def list_active_alerts(
    _user: str = Depends(require_permission(_PERM)),
):
    out = []
    for a in get_alerts_engine().active_alerts():
        out.append(ActiveAlertOut(
            rule=a.rule, metric=a.metric, severity=a.severity,
            last_value=a.last_value, fired_at=a.fired_at,
            payload=a.payload,
        ))
    return out


@router.get("/traces/sample")
async def traces_sample(
    _user: str = Depends(require_permission(_PERM)),
):
    tid = current_trace_id()
    return {"current_trace_id": tid, "active": tid is not None}
