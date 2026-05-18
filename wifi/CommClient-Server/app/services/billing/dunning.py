"""
Dunning / past-due workflow.

Walks every workspace whose newest invoice is overdue and emits the right
collection event:

    D+0   : notify        — email + webhook ``billing.invoice.due``
    D+3   : warning       — email + webhook ``billing.invoice.past_due``
    D+7   : degrade       — feature flags clipped to "free"-equivalent
    D+14  : suspend       — subscription status -> ``paused``
    D+30  : cancel        — subscription status -> ``canceled``

All transitions are idempotent and gated on
``Subscription.metadata_json["dunning"]["stage"]`` so re-running the job
doesn't repeat work.

Hook integration: events are pushed through
``app.services.webhooks_v2.event_bus`` when present; failures fall back
to plain logging so dunning never blocks on transport.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.billing import Invoice, Subscription

logger = get_logger(__name__)


# Default per-stage schedule (days past due, stage name)
DEFAULT_STAGES: list[tuple[int, str]] = [
    (0, "notify"),
    (3, "warning"),
    (7, "degrade"),
    (14, "suspend"),
    (30, "cancel"),
]


# ───────────────────────────────────────────────────────────────────────
# Event bus glue (graceful)
# ───────────────────────────────────────────────────────────────────────


async def _emit(event: str, payload: dict[str, Any]) -> None:
    try:
        from app.services.webhooks_v2 import event_bus as wv2_bus  # type: ignore
        if hasattr(wv2_bus, "publish"):
            await wv2_bus.publish(event, payload)
            return
    except Exception:                                                     # noqa: BLE001
        pass
    logger.info("dunning.event %s %s", event, payload)


# ───────────────────────────────────────────────────────────────────────
# Per-stage actions
# ───────────────────────────────────────────────────────────────────────


async def _apply_stage(
    db: AsyncSession, sub: Subscription, stage: str, invoice: Invoice,
) -> None:
    meta = dict(sub.metadata_json or {})
    dun = dict(meta.get("dunning") or {})
    if dun.get("stage") == stage:
        return
    payload = {
        "workspace_id": sub.workspace_id,
        "subscription_id": sub.id,
        "invoice_id": invoice.id,
        "invoice_number": invoice.number,
        "stage": stage,
        "due_at": invoice.due_at.isoformat() if invoice.due_at else None,
        "total_cents": invoice.total_cents,
    }

    if stage == "notify":
        await _emit("billing.invoice.due", payload)
    elif stage == "warning":
        await _emit("billing.invoice.past_due", payload)
    elif stage == "degrade":
        # Mark subscription past_due to surface in dashboards
        if sub.status != "past_due":
            sub.status = "past_due"
        meta["degraded"] = True
        await _emit("billing.subscription.degraded", payload)
    elif stage == "suspend":
        sub.status = "paused"
        await _emit("billing.subscription.suspended", payload)
    elif stage == "cancel":
        sub.status = "canceled"
        sub.canceled_at = datetime.now(timezone.utc)
        await _emit("billing.subscription.canceled_overdue", payload)

    dun["stage"] = stage
    dun["entered_at"] = datetime.now(timezone.utc).isoformat()
    meta["dunning"] = dun
    sub.metadata_json = meta


# ───────────────────────────────────────────────────────────────────────
# Main runner
# ───────────────────────────────────────────────────────────────────────


def _resolve_schedule(sub: Subscription) -> list[tuple[int, str]]:
    override = (sub.metadata_json or {}).get("dunning_schedule")
    if not override or not isinstance(override, list):
        return DEFAULT_STAGES
    parsed: list[tuple[int, str]] = []
    for entry in override:
        try:
            parsed.append((int(entry["days"]), str(entry["stage"])))
        except Exception:                                               # noqa: BLE001
            continue
    return parsed or DEFAULT_STAGES


def _select_stage(
    schedule: list[tuple[int, str]], days_past_due: int,
) -> str | None:
    chosen: str | None = None
    for days, name in sorted(schedule):
        if days_past_due >= days:
            chosen = name
        else:
            break
    return chosen


async def run_dunning_cycle(db: AsyncSession) -> dict[str, int]:
    """Walks every open invoice and applies the appropriate stage."""
    now = datetime.now(timezone.utc)
    open_invoices = (await db.execute(
        select(Invoice).where(Invoice.status == "open")
    )).scalars().all()

    stats: dict[str, int] = {}
    for inv in open_invoices:
        if not inv.due_at or inv.due_at > now:
            continue
        days_past_due = (now - inv.due_at).days
        sub = (await db.execute(
            select(Subscription).where(Subscription.id == inv.subscription_id)
        )).scalar_one_or_none()
        if not sub or sub.status == "canceled":
            continue
        schedule = _resolve_schedule(sub)
        stage = _select_stage(schedule, days_past_due)
        if not stage:
            continue
        await _apply_stage(db, sub, stage, inv)
        stats[stage] = stats.get(stage, 0) + 1
    await db.commit()
    logger.info("dunning.cycle finished stats=%s", stats)
    return stats


async def mark_invoice_paid_and_recover(
    db: AsyncSession, inv: Invoice,
) -> None:
    """Reverse dunning side-effects when a payment finally arrives."""
    inv.status = "paid"
    inv.paid_at = datetime.now(timezone.utc)
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == inv.subscription_id)
    )).scalar_one_or_none()
    if sub:
        if sub.status in ("past_due", "paused"):
            sub.status = "active"
        meta = dict(sub.metadata_json or {})
        meta.pop("dunning", None)
        meta.pop("degraded", None)
        sub.metadata_json = meta
        await _emit("billing.subscription.recovered", {
            "workspace_id": sub.workspace_id,
            "subscription_id": sub.id,
            "invoice_id": inv.id,
        })
    await db.commit()
