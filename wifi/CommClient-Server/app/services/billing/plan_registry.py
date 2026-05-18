"""
Built-in plan catalogue and bootstrap helper.

Five default plans ship with Helen:

    free        — 5 users, basic messaging
    starter     — 25 users, integrations
    pro         — 100 users, calls + bots
    business    — 500 users, SSO + audit
    enterprise  — unlimited, white-glove

The bootstrap routine upserts these plans into ``billing_plans`` so a
fresh deployment has something the UI can display. Custom plans created
through the admin API are never overwritten — bootstrap only fills gaps.
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.billing import Plan

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Default plan definitions
# ───────────────────────────────────────────────────────────────────────


DEFAULT_PLANS: list[dict[str, Any]] = [
    {
        "slug": "free",
        "name": "Free",
        "description": "Personal projects and small teams getting started.",
        "price_monthly_cents": 0,
        "price_yearly_cents": 0,
        "currency": "USD",
        "trial_days": 0,
        "is_public": True,
        "sort_order": 10,
        "included_quotas": {
            "active_users": 5,
            "messages_sent": 10_000,
            "files_uploaded": 500,
            "storage_gb": 2,
            "ai_tokens": 0,
            "agent_minutes": 0,
            "webhook_deliveries": 1_000,
            "api_calls": 50_000,
        },
        "feature_flags": {
            "calls": True,
            "calls_group": False,
            "whiteboard": False,
            "ai_assistant": False,
            "bots": False,
            "webhooks": True,
            "sso": False,
            "audit_log": False,
            "compliance_pack": False,
            "branding": False,
            "priority_support": False,
        },
    },
    {
        "slug": "starter",
        "name": "Starter",
        "description": "Growing teams that need more reach and integrations.",
        "price_monthly_cents": 900,
        "price_yearly_cents": 9_000,
        "currency": "USD",
        "trial_days": 14,
        "is_public": True,
        "sort_order": 20,
        "included_quotas": {
            "active_users": 25,
            "messages_sent": 100_000,
            "files_uploaded": 5_000,
            "storage_gb": 25,
            "ai_tokens": 50_000,
            "agent_minutes": 300,
            "webhook_deliveries": 25_000,
            "api_calls": 250_000,
        },
        "feature_flags": {
            "calls": True,
            "calls_group": True,
            "whiteboard": True,
            "ai_assistant": True,
            "bots": True,
            "webhooks": True,
            "sso": False,
            "audit_log": True,
            "compliance_pack": False,
            "branding": False,
            "priority_support": False,
        },
    },
    {
        "slug": "pro",
        "name": "Pro",
        "description": "Power users with full collaboration tooling.",
        "price_monthly_cents": 2_900,
        "price_yearly_cents": 29_000,
        "currency": "USD",
        "trial_days": 14,
        "is_public": True,
        "sort_order": 30,
        "included_quotas": {
            "active_users": 100,
            "messages_sent": 1_000_000,
            "files_uploaded": 50_000,
            "storage_gb": 250,
            "ai_tokens": 1_000_000,
            "agent_minutes": 3_000,
            "webhook_deliveries": 250_000,
            "api_calls": 2_500_000,
        },
        "feature_flags": {
            "calls": True,
            "calls_group": True,
            "whiteboard": True,
            "ai_assistant": True,
            "bots": True,
            "webhooks": True,
            "sso": True,
            "audit_log": True,
            "compliance_pack": False,
            "branding": True,
            "priority_support": False,
            "plugins": True,
            "analytics": True,
        },
    },
    {
        "slug": "business",
        "name": "Business",
        "description": "Mid-market: SSO, compliance, and white-label.",
        "price_monthly_cents": 9_900,
        "price_yearly_cents": 99_000,
        "currency": "USD",
        "trial_days": 30,
        "is_public": True,
        "sort_order": 40,
        "included_quotas": {
            "active_users": 500,
            "messages_sent": 10_000_000,
            "files_uploaded": 500_000,
            "storage_gb": 2_000,
            "ai_tokens": 10_000_000,
            "agent_minutes": 30_000,
            "webhook_deliveries": 2_500_000,
            "api_calls": 25_000_000,
        },
        "feature_flags": {
            "calls": True,
            "calls_group": True,
            "whiteboard": True,
            "ai_assistant": True,
            "bots": True,
            "webhooks": True,
            "sso": True,
            "audit_log": True,
            "compliance_pack": True,
            "branding": True,
            "priority_support": True,
            "plugins": True,
            "analytics": True,
            "warehouse_export": True,
            "federation": True,
        },
    },
    {
        "slug": "enterprise",
        "name": "Enterprise",
        "description": "Custom contracts, unlimited scale, dedicated support.",
        "price_monthly_cents": 0,    # negotiated
        "price_yearly_cents": 0,
        "currency": "USD",
        "trial_days": 0,
        "is_public": False,
        "sort_order": 50,
        "included_quotas": {
            "active_users": -1,
            "messages_sent": -1,
            "files_uploaded": -1,
            "storage_gb": -1,
            "ai_tokens": -1,
            "agent_minutes": -1,
            "webhook_deliveries": -1,
            "api_calls": -1,
        },
        "feature_flags": {
            "calls": True,
            "calls_group": True,
            "whiteboard": True,
            "ai_assistant": True,
            "bots": True,
            "webhooks": True,
            "sso": True,
            "audit_log": True,
            "compliance_pack": True,
            "branding": True,
            "priority_support": True,
            "plugins": True,
            "analytics": True,
            "warehouse_export": True,
            "federation": True,
            "edge_compute": True,
            "zero_trust": True,
            "dedicated_instance": True,
        },
    },
]


def quota_limit_for(plan: Plan, metric: str) -> int | float:
    """Return the configured quota (–1 = unlimited, 0 = disabled)."""
    if not plan or not plan.included_quotas:
        return 0
    val = plan.included_quotas.get(metric, 0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0


def feature_enabled(plan: Plan, flag: str) -> bool:
    """True if ``flag`` is set to a truthy value in the plan's feature map."""
    if not plan or not plan.feature_flags:
        return False
    return bool(plan.feature_flags.get(flag, False))


# ───────────────────────────────────────────────────────────────────────
# Bootstrap
# ───────────────────────────────────────────────────────────────────────


async def bootstrap_default_plans(
    db: AsyncSession,
    *,
    plans: Iterable[dict[str, Any]] | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    """Insert default plans that don't already exist.

    Args:
        db: open async session
        plans: override DEFAULT_PLANS
        overwrite: when True, update existing rows in-place with seed values
    Returns:
        ``{"inserted": N, "updated": M, "skipped": K}``
    """
    seeds = list(plans) if plans is not None else DEFAULT_PLANS
    inserted = updated = skipped = 0

    for seed in seeds:
        existing = (await db.execute(
            select(Plan).where(Plan.slug == seed["slug"])
        )).scalar_one_or_none()

        if existing is None:
            p = Plan(
                slug=seed["slug"],
                name=seed["name"],
                description=seed.get("description"),
                price_monthly_cents=seed.get("price_monthly_cents", 0),
                price_yearly_cents=seed.get("price_yearly_cents", 0),
                currency=seed.get("currency", "USD"),
                trial_days=seed.get("trial_days", 0),
                is_public=seed.get("is_public", True),
                sort_order=seed.get("sort_order", 0),
                included_quotas=seed.get("included_quotas", {}),
                feature_flags=seed.get("feature_flags", {}),
            )
            db.add(p)
            inserted += 1
        elif overwrite:
            existing.name = seed["name"]
            existing.description = seed.get("description")
            existing.price_monthly_cents = seed.get("price_monthly_cents", 0)
            existing.price_yearly_cents = seed.get("price_yearly_cents", 0)
            existing.currency = seed.get("currency", "USD")
            existing.trial_days = seed.get("trial_days", 0)
            existing.is_public = seed.get("is_public", True)
            existing.sort_order = seed.get("sort_order", 0)
            existing.included_quotas = seed.get("included_quotas", {})
            existing.feature_flags = seed.get("feature_flags", {})
            updated += 1
        else:
            skipped += 1

    await db.commit()
    logger.info(
        "billing.plans.bootstrap inserted=%s updated=%s skipped=%s",
        inserted, updated, skipped,
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
