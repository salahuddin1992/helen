"""
Phase 6 / Module AF — outbound event bus.

``publish(event_type, payload, workspace_id=None)`` is the single entry
point. It iterates over matching subscriptions, applies their filter
expression, and forwards each match to the delivery engine.

Filter language (JSON-only)::

    { "channel_id": "$eq:C-123" }
    { "user_id":    "$in:u1,u2,u3" }
    { "priority":   "$gte:5" }
    { "tag":        "$regex:^urgent" }

Multiple keys are AND-combined. Missing keys in the payload short-circuit
to ``False``.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.webhook_v2 import WebhookSubscription
from app.services.webhooks_v2.delivery_engine import delivery_engine

logger = get_logger(__name__)


# Canonical list of event types (router exposes this).
KNOWN_EVENT_TYPES: tuple[str, ...] = (
    "message.created", "message.edited", "message.deleted",
    "channel.created", "channel.updated", "channel.deleted",
    "user.joined", "user.left",
    "call.started", "call.ended",
    "file.uploaded",
    "agent.online", "agent.offline", "agent.command_completed",
    "bridge.message",
    "ai.session_created", "ai.message_appended",
    "dr.backup_succeeded", "dr.backup_failed",
    "dr.drill_failed",
    "compliance.export_ready", "compliance.deletion_completed",
)


# ── filter evaluator ────────────────────────────────────────────


_OP_RE = re.compile(r"^\$(eq|ne|in|nin|gte|lte|gt|lt|regex|contains|startswith):(.*)$")


def _matches(payload: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    if not filters:
        return True
    for key, rule in filters.items():
        value = payload.get(key)
        if isinstance(rule, str) and rule.startswith("$"):
            m = _OP_RE.match(rule)
            if not m:
                return False
            op, raw = m.group(1), m.group(2)
            if op == "eq":
                if str(value) != raw:
                    return False
            elif op == "ne":
                if str(value) == raw:
                    return False
            elif op == "in":
                opts = [s.strip() for s in raw.split(",")]
                if str(value) not in opts:
                    return False
            elif op == "nin":
                opts = [s.strip() for s in raw.split(",")]
                if str(value) in opts:
                    return False
            elif op in ("gte", "lte", "gt", "lt"):
                try:
                    a, b = float(value), float(raw)
                except (TypeError, ValueError):
                    return False
                if op == "gte" and not (a >= b): return False
                if op == "lte" and not (a <= b): return False
                if op == "gt"  and not (a >  b): return False
                if op == "lt"  and not (a <  b): return False
            elif op == "regex":
                try:
                    if not re.search(raw, str(value or "")):
                        return False
                except re.error:
                    return False
            elif op == "contains":
                if raw not in str(value or ""):
                    return False
            elif op == "startswith":
                if not str(value or "").startswith(raw):
                    return False
            else:
                return False
        else:
            # plain equality
            if value != rule:
                return False
    return True


# ── public API ─────────────────────────────────────────────────


async def publish(
    event_type: str,
    payload: Dict[str, Any],
    *,
    workspace_id: Optional[str] = None,
) -> int:
    """Forward this event to every matching subscription.

    Returns the number of deliveries queued.
    """
    enriched = dict(payload or {})
    enriched.setdefault("event_type", event_type)
    enriched.setdefault("event_id", uuid.uuid4().hex)
    enriched.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    if workspace_id and "workspace_id" not in enriched:
        enriched["workspace_id"] = workspace_id

    async with async_session_factory() as db:
        q = select(WebhookSubscription).where(WebhookSubscription.enabled.is_(True))
        if workspace_id:
            q = q.where(
                (WebhookSubscription.workspace_id == workspace_id)
                | (WebhookSubscription.workspace_id.is_(None))
            )
        subs = (await db.execute(q)).scalars().all()

    queued = 0
    for sub in subs:
        try:
            events = sub.events or []
            if events and event_type not in events and "*" not in events:
                continue
            if not _matches(enriched, sub.filters or {}):
                continue
            if sub.disabled_until and sub.disabled_until > datetime.now(timezone.utc):
                continue
            await delivery_engine.enqueue(
                subscription_id=sub.id,
                url=sub.url, secret=sub.secret,
                event_type=event_type,
                payload=enriched,
            )
            queued += 1
        except Exception as e:                                                  # pragma: no cover
            logger.warning("webhook_publish_skip", sub_id=sub.id, error=str(e))
    return queued
