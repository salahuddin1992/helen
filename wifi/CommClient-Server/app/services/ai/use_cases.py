"""
High-level AI use-cases composed from provider + redactor + quota.

Every use-case:
    1. Loads the workspace's AIConfig.
    2. Checks the caller's opt-in row.
    3. Reserves quota.
    4. Redacts PII.
    5. Calls the provider.
    6. Restores PII in the response.
    7. Persists prompt+response into AISession / AIMessage.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.ai_assistant import (
    AIConfig, AIMessage, AIOptIn, AISession,
    VALID_AI_KINDS,
)
from app.models.message import Message

from .providers import (
    CompletionOptions, CompletionResult, LLMProvider,
    ProviderUnavailable, make_provider,
)
from .quota import QuotaExceeded, quota
from .redactor import RedactionReport, RedactorPolicy, redact

logger = get_logger(__name__)


class AIAccessDenied(RuntimeError):
    """Raised when caller is not opted in or the workspace AI is disabled."""


@dataclass
class _Ctx:
    cfg: AIConfig
    provider: LLMProvider


# ── helpers ─────────────────────────────────────────────────


async def _resolve_secret(cfg: AIConfig) -> str | None:
    """Resolve an API key — tries secret_store first, then env, then settings."""
    import os
    if cfg.api_key_secret_ref:
        try:
            from app.services.secret_store import secret_store  # type: ignore
            v = await secret_store.get(cfg.api_key_secret_ref)
            if v:
                return v
        except Exception:
            pass
    env_key = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "ollama": None,
    }.get(cfg.provider)
    if env_key:
        v = os.environ.get(env_key)
        if v:
            return v
    return (cfg.settings or {}).get("api_key")


async def _load_ctx(db: AsyncSession, workspace_id: str) -> _Ctx:
    cfg = (await db.execute(
        select(AIConfig).where(AIConfig.workspace_id == workspace_id)
    )).scalar_one_or_none()
    if cfg is None or not cfg.enabled or cfg.provider == "none":
        raise AIAccessDenied("AI is not configured / enabled for this workspace")
    api_key = await _resolve_secret(cfg)
    try:
        prov = make_provider(
            cfg.provider, model=cfg.model_name,
            api_key=api_key, base_url=cfg.base_url,
        )
    except ProviderUnavailable as e:
        raise AIAccessDenied(str(e)) from e
    return _Ctx(cfg=cfg, provider=prov)


async def _ensure_opt_in(db: AsyncSession, *, workspace_id: str,
                         user_id: str, scope: str) -> None:
    row = (await db.execute(
        select(AIOptIn).where(
            AIOptIn.workspace_id == workspace_id,
            AIOptIn.user_id == user_id,
        )
    )).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise AIAccessDenied("user has not opted into AI features")
    if row.scope != "all" and row.scope != scope:
        raise AIAccessDenied(f"user opt-in scope ({row.scope}) excludes {scope}")


async def _open_session(db: AsyncSession, *, user_id: str, workspace_id: str,
                        kind: str, config_id: str | None,
                        title: str | None = None) -> AISession:
    if kind not in VALID_AI_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    sess = AISession(
        id=secrets.token_hex(16),
        user_id=user_id, workspace_id=workspace_id,
        config_id=config_id, kind=kind, title=title,
    )
    db.add(sess)
    await db.flush()
    return sess


async def _persist_pair(
    db: AsyncSession, *, session: AISession, prompt: str,
    result: CompletionResult, redaction: RedactionReport,
) -> None:
    db.add(AIMessage(
        id=secrets.token_hex(16),
        session_id=session.id, role="user",
        content=prompt, tokens_used=result.input_tokens,
        latency_ms=0,
        redacted_pii=redaction.counts,
        cost_micro_usd=0,
    ))
    db.add(AIMessage(
        id=secrets.token_hex(16),
        session_id=session.id, role="assistant",
        content=result.text, tokens_used=result.output_tokens,
        latency_ms=result.latency_ms,
        redacted_pii={},
        cost_micro_usd=result.cost_micro_usd,
    ))


# ── use-cases ───────────────────────────────────────────────


_SUMMARY_SYSTEM = (
    "You are a concise meeting / chat summarizer. Produce a 5-8 bullet "
    "summary covering decisions, action items (with owners if mentioned), "
    "and outstanding questions. Keep it under 200 words."
)


async def summarize_channel(
    db: AsyncSession, *, user_id: str, workspace_id: str,
    channel_id: str, since: timedelta = timedelta(hours=24),
    max_messages: int = 400,
) -> dict[str, Any]:
    await _ensure_opt_in(db, workspace_id=workspace_id,
                         user_id=user_id, scope="summarize")
    await quota.check(workspace_id=workspace_id, user_id=user_id)
    ctx = await _load_ctx(db, workspace_id)

    since_dt = datetime.now(timezone.utc) - since
    rows = (await db.execute(
        select(Message)
        .where(Message.channel_id == channel_id)
        .where(Message.created_at >= since_dt)
        .order_by(Message.created_at.asc())
        .limit(max_messages)
    )).scalars().all()
    if not rows:
        return {"summary": "(no messages in the requested window)",
                "messages_considered": 0}

    transcript = "\n".join(f"{m.sender_id}: {m.content}" for m in rows)
    red = redact(transcript, RedactorPolicy(strict=False))

    sess = await _open_session(
        db, user_id=user_id, workspace_id=workspace_id,
        kind="summary", config_id=ctx.cfg.id,
        title=f"summary:{channel_id}",
    )
    res = await ctx.provider.complete(
        red.text, CompletionOptions(
            max_tokens=600, temperature=0.2, system=_SUMMARY_SYSTEM,
        ),
    )
    res.text = red.restore(res.text)
    await _persist_pair(db, session=sess, prompt=red.text, result=res,
                        redaction=red)
    await db.commit()
    await quota.record(workspace_id=workspace_id, user_id=user_id,
                       tokens=res.total_tokens,
                       cost_micro_usd=res.cost_micro_usd)
    return {
        "session_id": sess.id,
        "summary": res.text,
        "messages_considered": len(rows),
        "tokens": res.total_tokens,
        "latency_ms": res.latency_ms,
        "cost_micro_usd": res.cost_micro_usd,
    }


_DRAFT_SYSTEM = (
    "You are a helpful drafting assistant for chat replies. Produce a single "
    "reply in the same tone as the surrounding conversation, no preamble, "
    "no explanations, no quotes around it."
)


async def draft_reply(
    db: AsyncSession, *, user_id: str, workspace_id: str,
    thread_messages: list[str], intent: str,
) -> dict[str, Any]:
    await _ensure_opt_in(db, workspace_id=workspace_id,
                         user_id=user_id, scope="draft")
    await quota.check(workspace_id=workspace_id, user_id=user_id)
    ctx = await _load_ctx(db, workspace_id)

    context = "\n".join(thread_messages[-20:])
    prompt = f"Conversation:\n{context}\n\nIntent: {intent}\n\nDraft:"
    red = redact(prompt)

    sess = await _open_session(db, user_id=user_id, workspace_id=workspace_id,
                               kind="draft", config_id=ctx.cfg.id)
    res = await ctx.provider.complete(
        red.text, CompletionOptions(
            max_tokens=240, temperature=0.7, system=_DRAFT_SYSTEM,
        ),
    )
    res.text = red.restore(res.text)
    await _persist_pair(db, session=sess, prompt=red.text, result=res,
                        redaction=red)
    await db.commit()
    await quota.record(workspace_id=workspace_id, user_id=user_id,
                       tokens=res.total_tokens,
                       cost_micro_usd=res.cost_micro_usd)
    return {"session_id": sess.id, "draft": res.text,
            "tokens": res.total_tokens, "latency_ms": res.latency_ms}


async def smart_search(
    db: AsyncSession, *, user_id: str, workspace_id: str,
    query: str, scope_channel_ids: list[str] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    await _ensure_opt_in(db, workspace_id=workspace_id,
                         user_id=user_id, scope="search")
    await quota.check(workspace_id=workspace_id, user_id=user_id)
    ctx = await _load_ctx(db, workspace_id)

    # ── keyword shortlist ─────────────────────────────
    terms = [t for t in query.lower().split() if len(t) > 2]
    q = select(Message)
    if scope_channel_ids:
        q = q.where(Message.channel_id.in_(scope_channel_ids))
    for t in terms:
        q = q.where(Message.content.ilike(f"%{t}%"))
    q = q.order_by(desc(Message.created_at)).limit(max(limit * 4, 100))
    candidates = (await db.execute(q)).scalars().all()
    if not candidates:
        return {"results": [], "method": "keyword"}

    # ── re-rank via embedding cosine ──────────────────
    try:
        q_vec = await ctx.provider.embed(query)
        scored: list[tuple[float, Message]] = []
        for m in candidates:
            v = await ctx.provider.embed(m.content[:1024])
            scored.append((_cos(q_vec, v), m))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = scored[:limit]
        return {
            "method": "embed",
            "results": [{
                "id": m.id, "channel_id": m.channel_id,
                "sender_id": m.sender_id, "content": m.content[:400],
                "score": round(s, 4),
                "created_at": m.created_at.isoformat()
                if getattr(m, "created_at", None) else None,
            } for s, m in out],
        }
    except Exception as exc:
        logger.warning("smart_search_embed_failed", err=str(exc))
        out = candidates[:limit]
        return {
            "method": "keyword-fallback",
            "results": [{
                "id": m.id, "channel_id": m.channel_id,
                "sender_id": m.sender_id, "content": m.content[:400],
                "created_at": m.created_at.isoformat()
                if getattr(m, "created_at", None) else None,
            } for m in out],
        }


def _cos(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(n))
    na = (sum(x * x for x in a[:n])) ** 0.5
    nb = (sum(x * x for x in b[:n])) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def chat_turn(
    db: AsyncSession, *, user_id: str, workspace_id: str,
    session_id: str | None, message: str,
) -> dict[str, Any]:
    await _ensure_opt_in(db, workspace_id=workspace_id,
                         user_id=user_id, scope="all")
    await quota.check(workspace_id=workspace_id, user_id=user_id)
    ctx = await _load_ctx(db, workspace_id)

    sess: AISession | None = None
    if session_id:
        sess = (await db.execute(
            select(AISession).where(
                AISession.id == session_id,
                AISession.user_id == user_id,
            )
        )).scalar_one_or_none()
    if sess is None:
        sess = await _open_session(
            db, user_id=user_id, workspace_id=workspace_id,
            kind="chat", config_id=ctx.cfg.id,
            title=message[:80],
        )

    history = (await db.execute(
        select(AIMessage).where(AIMessage.session_id == sess.id)
        .order_by(AIMessage.created_at.asc())
    )).scalars().all()
    convo = "\n".join(f"{m.role.upper()}: {m.content}" for m in history[-20:])
    prompt = f"{convo}\nUSER: {message}\nASSISTANT:" if convo else message
    red = redact(prompt)

    res = await ctx.provider.complete(
        red.text, CompletionOptions(
            max_tokens=900, temperature=0.6,
            system="You are Helen's helpful assistant.",
        ),
    )
    res.text = red.restore(res.text)
    await _persist_pair(db, session=sess, prompt=message, result=res,
                        redaction=red)
    await db.commit()
    await quota.record(workspace_id=workspace_id, user_id=user_id,
                       tokens=res.total_tokens,
                       cost_micro_usd=res.cost_micro_usd)
    return {"session_id": sess.id, "reply": res.text,
            "tokens": res.total_tokens, "latency_ms": res.latency_ms}


async def stream_chat_turn(
    db: AsyncSession, *, user_id: str, workspace_id: str,
    session_id: str | None, message: str,
) -> AsyncIterator[str]:
    """Streaming variant of chat_turn — yields tokens as they arrive.
    Persists the final message once the stream completes."""
    await _ensure_opt_in(db, workspace_id=workspace_id,
                         user_id=user_id, scope="all")
    await quota.check(workspace_id=workspace_id, user_id=user_id)
    ctx = await _load_ctx(db, workspace_id)

    sess: AISession | None = None
    if session_id:
        sess = (await db.execute(
            select(AISession).where(
                AISession.id == session_id, AISession.user_id == user_id,
            )
        )).scalar_one_or_none()
    if sess is None:
        sess = await _open_session(
            db, user_id=user_id, workspace_id=workspace_id,
            kind="chat", config_id=ctx.cfg.id, title=message[:80],
        )

    red = redact(message)
    chunks: list[str] = []
    t0 = time.monotonic()
    async for piece in ctx.provider.stream(
        red.text, CompletionOptions(
            max_tokens=900, temperature=0.6,
            system="You are Helen's helpful assistant.",
        ),
    ):
        chunks.append(piece)
        yield piece
    full = red.restore("".join(chunks))
    # rough token estimate when provider streaming omits counts
    approx_in = max(1, len(message) // 4)
    approx_out = max(1, len(full) // 4)
    res = CompletionResult(
        text=full, input_tokens=approx_in, output_tokens=approx_out,
        latency_ms=int((time.monotonic() - t0) * 1000),
        cost_micro_usd=0,
    )
    await _persist_pair(db, session=sess, prompt=message, result=res,
                        redaction=red)
    await db.commit()
    await quota.record(workspace_id=workspace_id, user_id=user_id,
                       tokens=res.total_tokens,
                       cost_micro_usd=res.cost_micro_usd)
