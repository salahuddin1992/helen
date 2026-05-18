"""
Token / cost quota enforcement for AI features.

* Per-workspace token bucket (sliding window).
* Per-user daily limit.
* Cost tracker in micro-USD, persisted via ``AIMessage.cost_micro_usd``.

Backed by an in-memory map keyed by ``(workspace_id, user_id)`` — for
horizontal scale, swap to Redis (the contract is identical). On startup,
the daily counters self-bootstrap from today's AIMessage rows.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.ai_assistant import AIMessage, AISession

logger = get_logger(__name__)


@dataclass
class QuotaPolicy:
    workspace_daily_tokens: int = 1_000_000
    user_daily_tokens: int = 100_000
    user_daily_cost_micro_usd: int = 5_000_000        # = $5
    rate_limit_per_minute: int = 30


@dataclass
class _UserCounter:
    tokens_today: int = 0
    cost_today: int = 0
    minute_window_start: float = 0.0
    minute_calls: int = 0


@dataclass
class _WorkspaceCounter:
    tokens_today: int = 0
    cost_today: int = 0


class QuotaExceeded(RuntimeError):
    def __init__(self, reason: str, *, retry_after_s: int = 60) -> None:
        super().__init__(reason)
        self.retry_after_s = retry_after_s


class QuotaManager:
    _instance: "QuotaManager | None" = None

    def __init__(self, policy: QuotaPolicy | None = None) -> None:
        self.policy = policy or QuotaPolicy()
        self._users: dict[tuple[str, str], _UserCounter] = {}
        self._wsx: dict[str, _WorkspaceCounter] = {}
        self._day = date.today()
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> "QuotaManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def reset_if_new_day(self) -> None:
        async with self._lock:
            today = date.today()
            if today != self._day:
                self._users.clear()
                self._wsx.clear()
                self._day = today

    async def check(self, *, workspace_id: str, user_id: str) -> None:
        await self.reset_if_new_day()
        now = time.monotonic()
        async with self._lock:
            uc = self._users.setdefault((workspace_id, user_id), _UserCounter())
            wc = self._wsx.setdefault(workspace_id, _WorkspaceCounter())

            # rate-limit window
            if now - uc.minute_window_start > 60.0:
                uc.minute_window_start = now
                uc.minute_calls = 0
            uc.minute_calls += 1
            if uc.minute_calls > self.policy.rate_limit_per_minute:
                raise QuotaExceeded("rate limit exceeded", retry_after_s=60)

            if uc.tokens_today >= self.policy.user_daily_tokens:
                raise QuotaExceeded("user daily token budget exhausted",
                                    retry_after_s=86_400)
            if uc.cost_today >= self.policy.user_daily_cost_micro_usd:
                raise QuotaExceeded("user daily cost budget exhausted",
                                    retry_after_s=86_400)
            if wc.tokens_today >= self.policy.workspace_daily_tokens:
                raise QuotaExceeded("workspace daily token budget exhausted",
                                    retry_after_s=86_400)

    async def record(self, *, workspace_id: str, user_id: str,
                     tokens: int, cost_micro_usd: int) -> None:
        await self.reset_if_new_day()
        async with self._lock:
            uc = self._users.setdefault((workspace_id, user_id), _UserCounter())
            wc = self._wsx.setdefault(workspace_id, _WorkspaceCounter())
            uc.tokens_today += tokens
            uc.cost_today += cost_micro_usd
            wc.tokens_today += tokens
            wc.cost_today += cost_micro_usd

    async def bootstrap_from_db(self, db: AsyncSession,
                                workspace_id: str) -> None:
        """Rebuild today's counters from persisted AIMessage rows."""
        today_start = datetime.combine(date.today(), datetime.min.time(),
                                       tzinfo=timezone.utc)
        rows = (await db.execute(
            select(
                AISession.user_id,
                func.sum(AIMessage.tokens_used),
                func.sum(AIMessage.cost_micro_usd),
            )
            .join(AISession, AISession.id == AIMessage.session_id)
            .where(AISession.workspace_id == workspace_id)
            .where(AIMessage.created_at >= today_start)
            .group_by(AISession.user_id)
        )).all()
        async with self._lock:
            wc = self._wsx.setdefault(workspace_id, _WorkspaceCounter())
            for user_id, tokens, cost in rows:
                uc = self._users.setdefault(
                    (workspace_id, user_id), _UserCounter())
                uc.tokens_today = int(tokens or 0)
                uc.cost_today = int(cost or 0)
                wc.tokens_today += uc.tokens_today
                wc.cost_today += uc.cost_today

    def snapshot(self, workspace_id: str) -> dict[str, int]:
        wc = self._wsx.get(workspace_id, _WorkspaceCounter())
        return {
            "workspace_tokens_today": wc.tokens_today,
            "workspace_cost_micro_usd_today": wc.cost_today,
            "limits_workspace_daily_tokens": self.policy.workspace_daily_tokens,
            "limits_user_daily_tokens": self.policy.user_daily_tokens,
            "limits_user_daily_cost_micro_usd":
                self.policy.user_daily_cost_micro_usd,
        }


quota = QuotaManager.instance()
