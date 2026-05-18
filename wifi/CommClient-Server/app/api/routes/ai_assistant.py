"""
Phase 5 / Module Z — AI Assistant REST endpoints.

All routes require:
    * a valid access token (via require_permission("ai.use") or per-route),
    * an explicit ``AIOptIn`` row for the caller in the workspace.

Streaming chat is exposed via WS at ``/api/ai/stream``.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db, get_current_user_id
from app.core.logging import get_logger
from app.core.security import decode_token
from app.models.ai_assistant import (
    AIConfig, AIOptIn, VALID_AI_SCOPES,
)
from app.services.ai.providers import installed_providers
from app.services.ai.quota import QuotaExceeded, quota
from app.services.ai.redactor import PIILeakBlocked
from app.services.ai.use_cases import (
    AIAccessDenied,
    chat_turn,
    draft_reply,
    smart_search,
    stream_chat_turn,
    summarize_channel,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai-assistant"])


# ── shapes ──────────────────────────────────────────────────


class OptInIn(BaseModel):
    workspace_id: str
    scope: str = Field(default="all")


class SummarizeIn(BaseModel):
    workspace_id: str
    since_hours: int = Field(default=24, ge=1, le=168)


class SearchIn(BaseModel):
    workspace_id: str
    query: str = Field(min_length=1, max_length=2048)
    channel_ids: Optional[list[str]] = None
    limit: int = Field(default=25, ge=1, le=100)


class DraftIn(BaseModel):
    workspace_id: str
    thread_messages: list[str]
    intent: str = Field(min_length=1, max_length=512)


class ChatIn(BaseModel):
    workspace_id: str
    session_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=8000)


# ── status / opt-in ────────────────────────────────────────


@router.get("/status")
async def ai_status(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    cfg = (await db.execute(
        select(AIConfig).where(AIConfig.workspace_id == workspace_id)
    )).scalar_one_or_none()
    opt = (await db.execute(
        select(AIOptIn).where(
            AIOptIn.workspace_id == workspace_id,
            AIOptIn.user_id == user_id,
        )
    )).scalar_one_or_none()
    return {
        "enabled": bool(cfg and cfg.enabled),
        "provider": cfg.provider if cfg else "none",
        "model": cfg.model_name if cfg else None,
        "installed_providers": installed_providers(),
        "opted_in": bool(opt and opt.revoked_at is None),
        "scope": opt.scope if (opt and opt.revoked_at is None) else None,
        "quota": quota.snapshot(workspace_id),
    }


@router.post("/opt-in")
async def opt_in(
    body: OptInIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    if body.scope not in VALID_AI_SCOPES:
        raise HTTPException(400, detail=f"invalid scope: {body.scope}")
    row = (await db.execute(
        select(AIOptIn).where(
            AIOptIn.workspace_id == body.workspace_id,
            AIOptIn.user_id == user_id,
        )
    )).scalar_one_or_none()
    if row is None:
        row = AIOptIn(
            id=secrets.token_hex(16),
            workspace_id=body.workspace_id,
            user_id=user_id, scope=body.scope,
        )
        db.add(row)
    else:
        row.scope = body.scope
        row.revoked_at = None
    await db.commit()
    audit_log("ai.opt_in", user_id=user_id, success=True,
              details={"workspace_id": body.workspace_id, "scope": body.scope})
    return {"ok": True, "scope": body.scope}


@router.post("/opt-out")
async def opt_out(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    row = (await db.execute(
        select(AIOptIn).where(
            AIOptIn.workspace_id == workspace_id,
            AIOptIn.user_id == user_id,
        )
    )).scalar_one_or_none()
    if row is not None:
        row.revoked_at = datetime.utcnow()
        await db.commit()
    audit_log("ai.opt_out", user_id=user_id, success=True,
              details={"workspace_id": workspace_id})
    return {"ok": True}


# ── feature endpoints ──────────────────────────────────────


def _wrap_errors(coro):
    """Translate domain errors into HTTPException."""
    async def _runner(*a, **kw):
        try:
            return await coro(*a, **kw)
        except AIAccessDenied as e:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(e))
        except QuotaExceeded as e:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e),
                headers={"Retry-After": str(e.retry_after_s)},
            )
        except PIILeakBlocked as e:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e))
    return _runner


@router.post("/summarize/channel/{channel_id}")
async def post_summarize_channel(
    channel_id: str,
    body: SummarizeIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    fn = _wrap_errors(summarize_channel)
    res = await fn(
        db, user_id=user_id, workspace_id=body.workspace_id,
        channel_id=channel_id, since=timedelta(hours=body.since_hours),
    )
    audit_log("ai.summarize", user_id=user_id, success=True,
              details={"channel_id": channel_id})
    return res


@router.post("/search")
async def post_search(
    body: SearchIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    fn = _wrap_errors(smart_search)
    res = await fn(
        db, user_id=user_id, workspace_id=body.workspace_id,
        query=body.query, scope_channel_ids=body.channel_ids,
        limit=body.limit,
    )
    audit_log("ai.search", user_id=user_id, success=True,
              details={"q": body.query[:60]})
    return res


@router.post("/draft")
async def post_draft(
    body: DraftIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    fn = _wrap_errors(draft_reply)
    res = await fn(
        db, user_id=user_id, workspace_id=body.workspace_id,
        thread_messages=body.thread_messages, intent=body.intent,
    )
    audit_log("ai.draft", user_id=user_id, success=True)
    return res


@router.post("/chat")
async def post_chat(
    body: ChatIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    fn = _wrap_errors(chat_turn)
    res = await fn(
        db, user_id=user_id, workspace_id=body.workspace_id,
        session_id=body.session_id, message=body.message,
    )
    audit_log("ai.chat", user_id=user_id, success=True,
              details={"session_id": res.get("session_id")})
    return res


# ── streaming WebSocket ────────────────────────────────────


@router.websocket("/stream")
async def ws_stream(ws: WebSocket):
    """Bidirectional WS for streaming chat turns.

    Expected JSON frames:
        client → {"workspace_id": "...", "session_id": null|str, "message": "..."}
        server → {"type":"chunk","data":"..."} ...
        server → {"type":"done"}
        server → {"type":"error","detail":"..."}
    """
    await ws.accept()
    try:
        # validate bearer token from query or header
        token = ws.query_params.get("token") or ""
        if not token:
            await ws.send_json({"type": "error", "detail": "missing token"})
            await ws.close(code=4401)
            return
        try:
            payload = decode_token(token)
        except Exception:
            await ws.send_json({"type": "error", "detail": "invalid token"})
            await ws.close(code=4401)
            return
        user_id = payload.get("sub")
        if not user_id or payload.get("type") != "access":
            await ws.send_json({"type": "error", "detail": "bad token"})
            await ws.close(code=4401)
            return

        # acquire DB session
        from app.core.deps import get_db_session
        async for db in get_db_session():
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    await ws.send_json({"type": "error",
                                        "detail": "invalid json"})
                    continue
                wsx = msg.get("workspace_id")
                sid = msg.get("session_id")
                text = msg.get("message")
                if not wsx or not text:
                    await ws.send_json({"type": "error",
                                        "detail": "workspace_id and message required"})
                    continue
                try:
                    async for piece in stream_chat_turn(
                        db, user_id=user_id, workspace_id=wsx,
                        session_id=sid, message=text,
                    ):
                        await ws.send_json({"type": "chunk", "data": piece})
                    await ws.send_json({"type": "done"})
                except AIAccessDenied as e:
                    await ws.send_json({"type": "error", "detail": str(e)})
                except QuotaExceeded as e:
                    await ws.send_json({"type": "error",
                                        "detail": str(e),
                                        "retry_after_s": e.retry_after_s})
                except Exception as e:
                    logger.error("ai_ws_stream_error", err=str(e))
                    await ws.send_json({"type": "error", "detail": str(e)})
    except Exception as e:
        logger.error("ai_ws_close", err=str(e))
    finally:
        try:
            await ws.close()
        except Exception:
            pass
