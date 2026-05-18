"""
Phase 5 / Module Z — AI admin REST endpoints.

Mounted under ``/api/admin/ai``. Requires the ``ai.manage`` permission.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.ai_assistant import (
    AIConfig, AIMessage, AIOptIn, AISession,
    VALID_AI_PROVIDERS,
)
from app.services.ai.providers import installed_providers
from app.services.ai.quota import quota
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/ai", tags=["admin-ai"])

_PERM = "ai.manage"


# ── shapes ──────────────────────────────────────────────────


class AIConfigIn(BaseModel):
    workspace_id: str
    provider: str = Field(..., description="anthropic|openai|ollama|none")
    model_name: str
    api_key_secret_ref: Optional[str] = None
    api_key_value: Optional[str] = Field(
        default=None,
        description="If supplied, server stores via secret_store and clears this from settings.",
    )
    base_url: Optional[str] = None
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class AIConfigOut(BaseModel):
    id: str
    workspace_id: str
    provider: str
    model_name: str
    api_key_secret_ref: Optional[str]
    base_url: Optional[str]
    enabled: bool
    settings: dict[str, Any]


def _mask(s: dict[str, Any]) -> dict[str, Any]:
    out = dict(s or {})
    if "api_key" in out and out["api_key"]:
        out["api_key"] = "***"
    return out


# ── config CRUD ─────────────────────────────────────────────


@router.get("/config", response_model=AIConfigOut)
async def get_config(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    cfg = (await db.execute(
        select(AIConfig).where(AIConfig.workspace_id == workspace_id)
    )).scalar_one_or_none()
    if cfg is None:
        raise HTTPException(404, "no AI config for workspace")
    return AIConfigOut(
        id=cfg.id, workspace_id=cfg.workspace_id, provider=cfg.provider,
        model_name=cfg.model_name,
        api_key_secret_ref=cfg.api_key_secret_ref, base_url=cfg.base_url,
        enabled=cfg.enabled, settings=_mask(cfg.settings or {}),
    )


@router.put("/config", response_model=AIConfigOut)
async def upsert_config(
    body: AIConfigIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.provider not in VALID_AI_PROVIDERS:
        raise HTTPException(400, f"invalid provider: {body.provider}")
    inst = installed_providers()
    if body.provider in ("anthropic", "openai") and not inst.get(body.provider):
        raise HTTPException(
            503, f"provider library not installed: {body.provider}",
        )

    cfg = (await db.execute(
        select(AIConfig).where(AIConfig.workspace_id == body.workspace_id)
    )).scalar_one_or_none()
    if cfg is None:
        cfg = AIConfig(
            id=secrets.token_hex(16),
            workspace_id=body.workspace_id,
        )
        db.add(cfg)

    cfg.provider = body.provider
    cfg.model_name = body.model_name
    cfg.base_url = body.base_url
    cfg.enabled = body.enabled
    settings = dict(body.settings or {})

    # api key handling: prefer secret_store
    if body.api_key_value:
        try:
            from app.services.secret_store import secret_store  # type: ignore
            ref = body.api_key_secret_ref or f"ai:{body.workspace_id}:apikey"
            await secret_store.set(ref, body.api_key_value)
            cfg.api_key_secret_ref = ref
            settings.pop("api_key", None)
        except Exception:
            # last-resort store in settings (still masked on read)
            settings["api_key"] = body.api_key_value
    elif body.api_key_secret_ref is not None:
        cfg.api_key_secret_ref = body.api_key_secret_ref

    cfg.settings = settings
    await db.commit()
    await db.refresh(cfg)

    audit_log("ai.config_upsert", user_id=user_id, success=True,
              details={"workspace_id": body.workspace_id,
                       "provider": body.provider})
    return AIConfigOut(
        id=cfg.id, workspace_id=cfg.workspace_id, provider=cfg.provider,
        model_name=cfg.model_name,
        api_key_secret_ref=cfg.api_key_secret_ref, base_url=cfg.base_url,
        enabled=cfg.enabled, settings=_mask(cfg.settings or {}),
    )


# ── usage / audit ──────────────────────────────────────────


@router.get("/usage")
async def usage(
    workspace_id: str,
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(
            func.date(AIMessage.created_at).label("d"),
            func.sum(AIMessage.tokens_used).label("tok"),
            func.sum(AIMessage.cost_micro_usd).label("cost"),
            func.count(AIMessage.id).label("n"),
        )
        .join(AISession, AISession.id == AIMessage.session_id)
        .where(AISession.workspace_id == workspace_id)
        .where(AIMessage.created_at >= since)
        .group_by(func.date(AIMessage.created_at))
        .order_by(func.date(AIMessage.created_at).asc())
    )).all()
    return {
        "workspace_id": workspace_id,
        "days": days,
        "series": [{
            "date": str(r.d), "tokens": int(r.tok or 0),
            "cost_micro_usd": int(r.cost or 0), "calls": int(r.n or 0),
        } for r in rows],
        "live_counters": quota.snapshot(workspace_id),
    }


@router.get("/audit")
async def audit(
    workspace_id: str,
    user_id: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = (select(AISession)
         .where(AISession.workspace_id == workspace_id))
    if user_id:
        q = q.where(AISession.user_id == user_id)
    if kind:
        q = q.where(AISession.kind == kind)
    q = q.order_by(desc(AISession.created_at)).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [{
        "id": r.id, "user_id": r.user_id, "kind": r.kind,
        "title": r.title, "config_id": r.config_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.get("/opt-ins")
async def list_opt_ins(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(AIOptIn).where(AIOptIn.workspace_id == workspace_id)
    )).scalars().all()
    return [{
        "user_id": r.user_id, "scope": r.scope,
        "opted_at": r.opted_at.isoformat() if r.opted_at else None,
        "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
    } for r in rows]


@router.post("/test")
async def test_prompt(
    workspace_id: str,
    prompt: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    from app.services.ai.providers import CompletionOptions
    from app.services.ai.use_cases import _load_ctx                    # type: ignore
    try:
        ctx = await _load_ctx(db, workspace_id)
    except Exception as e:
        raise HTTPException(400, f"cannot load provider: {e}")
    res = await ctx.provider.complete(
        prompt[:4096], CompletionOptions(max_tokens=256, temperature=0.2),
    )
    audit_log("ai.admin_test", user_id=user_id, success=True,
              details={"workspace_id": workspace_id,
                       "tokens": res.total_tokens})
    return {
        "text": res.text, "tokens": res.total_tokens,
        "latency_ms": res.latency_ms, "cost_micro_usd": res.cost_micro_usd,
    }
