"""Parallel multi-path send — race the fastest of K routes.

Use case: critical messages (mute/kick, lifecycle commands) where
we'd rather pay 2× bandwidth than wait for sequential failover.

Caller supplies an attempt callable + a list of route candidates.
We dispatch all in parallel and return the first to succeed, then
cancel the rest.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")
AttemptFn = Callable[[str], Awaitable[T]]


async def race_routes(
    target_ids: list[str],
    attempt: AttemptFn,
    *,
    deadline_sec: float = 5.0,
    cancel_pending: bool = True,
) -> tuple[bool, T | None, str]:
    """Return (ok, value, winner_target).

    Winner = the first target whose ``attempt`` returns non-None /
    truthy. Pending tasks are cancelled when the winner returns
    (unless ``cancel_pending=False``).
    """
    if not target_ids:
        return False, None, ""

    loop = asyncio.get_event_loop()
    tasks: list[tuple[asyncio.Task, str]] = [
        (
            loop.create_task(attempt(t), name=f"race-{t[:12]}"),
            t,
        )
        for t in target_ids
    ]
    done_set, pending_set = await asyncio.wait(
        [t for t, _ in tasks],
        timeout=deadline_sec,
        return_when=asyncio.FIRST_COMPLETED,
    )

    winner_target = ""
    winner_value: T | None = None
    success = False
    for done in done_set:
        try:
            r = done.result()
        except Exception as e:
            logger.debug("race_task_raised", error=str(e)[:80])
            continue
        if r:
            success = True
            winner_value = r
            for task, target in tasks:
                if task is done:
                    winner_target = target
                    break
            break

    if cancel_pending:
        for task in pending_set:
            task.cancel()

    return success, winner_value, winner_target
