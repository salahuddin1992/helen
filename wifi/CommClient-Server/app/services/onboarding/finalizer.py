"""
OnboardingFinalizer — atomic flip of ``onboarding_complete=true``.

Strategy
--------
The wizard's step apply path already commits side-effects per step. The
finalizer:

    1. Acquires the state machine lock.
    2. Verifies every step is in ``completed_steps``.
    3. Persists ``onboarding_state.locked=True`` and ``finalized_at``.
    4. Emits a single ``onboarding.finalized`` audit event.

If verification fails we return a per-step status dict; the operator can
go back and re-run the missing step. The DB transaction is rolled back
on any unexpected error so we never end up half-finalized.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.services.onboarding.state_machine import (
    OnboardingStateMachine, STEP_DEFINITIONS, TOTAL_STEPS,
)

logger = get_logger(__name__)


class OnboardingFinalizer:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.sm = OnboardingStateMachine(db)

    async def finalize(self, actor: str) -> dict[str, Any]:
        async with self.sm._lock:                      # noqa: SLF001
            state = await self.sm.get_state()
            if state.locked:
                return {"already_finalized": True, "ok": True}

            completed = set(state.completed_steps or [])
            status_per_step: list[dict[str, Any]] = []
            missing: list[int] = []
            for spec in STEP_DEFINITIONS:
                done = spec.num in completed
                status_per_step.append({
                    "step": spec.num,
                    "key": spec.key,
                    "title": spec.title,
                    "completed": done,
                })
                if not done and spec.num != TOTAL_STEPS:
                    # The final step itself is treated as the marker.
                    missing.append(spec.num)

            if missing:
                audit_log("onboarding.finalize", user_id=actor, success=False,
                          details={"missing_steps": missing})
                return {
                    "ok": False, "missing_steps": missing,
                    "status_per_step": status_per_step,
                }

            try:
                state.locked = True
                state.finalized_at = datetime.now(timezone.utc)
                state.actor_id = actor
                # Also mark step 14 done.
                done_list = list(state.completed_steps or [])
                if TOTAL_STEPS not in done_list:
                    done_list.append(TOTAL_STEPS)
                    done_list.sort()
                state.completed_steps = done_list
                await self.db.flush()
                await self.db.commit()
            except Exception as e:
                await self.db.rollback()
                audit_log("onboarding.finalize", user_id=actor, success=False,
                          details={"error": str(e)})
                logger.error("finalize_failed", error=str(e), exc_info=True)
                return {"ok": False, "error": str(e),
                        "status_per_step": status_per_step}

            audit_log("onboarding.finalized", user_id=actor,
                      details={"steps": TOTAL_STEPS})
            return {"ok": True, "finalized_at": state.finalized_at.isoformat(),
                    "status_per_step": status_per_step}
