"""Failover manager — combines breaker + classifier + retry policy.

Caller supplies an attempt callable + a list of candidate targets.
The manager:

  1. For each target, checks the breaker (skip if OPEN).
  2. Runs the attempt; classifies success/failure.
  3. On retryable failure: records breaker failure, moves on.
  4. On non-retryable failure (security/permanent): raises.
  5. On exhaustion: enqueues into the retry queue and raises.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Generic, TypeVar

from app.resilience.circuit_breaker import get_breaker_registry
from app.resilience.failure_classifier import (
    FailureKind, classify_exception, classify_status, is_retryable,
)
from app.resilience.resilience_events import emit
from app.resilience.resilience_exceptions import FailoverError
from app.resilience.retry_queue import get_retry_queue

T = TypeVar("T")

# Attempt signature: (target) -> awaitable[(ok, value, status_or_None)]
AttemptFn = Callable[[str], Awaitable[tuple[bool, T | None, int | None]]]


async def try_with_failover(
    targets: list[str],
    attempt: AttemptFn,
    *,
    task_kind_for_retry: str | None = None,
    retry_payload: dict | None = None,
) -> T:
    """Try each target in order; on first success return its value.

    Each target gets its own breaker check + outcome record. If all
    targets fail with retryable errors and ``task_kind_for_retry`` is
    provided, the call is enqueued for later retry before raising.
    """
    breaker = get_breaker_registry()
    last_kind = FailureKind.UNKNOWN
    attempted: list[str] = []
    for target in targets:
        if not breaker.allow(target):
            emit("failover.skipped_breaker_open", {"target": target})
            continue
        attempted.append(target)
        try:
            ok, value, status = await attempt(target)
        except BaseException as exc:
            kind = classify_exception(exc)
            breaker.record_failure(target)
            last_kind = kind
            if not is_retryable(kind):
                emit("failover.non_retryable", {
                    "target": target, "kind": kind.value,
                    "exc": type(exc).__name__,
                })
                raise FailoverError(
                    f"non-retryable {kind.value} from {target}: {exc}"
                )
            continue

        if ok:
            breaker.record_success(target)
            emit("failover.ok", {"target": target,
                                  "attempts": len(attempted)})
            return value  # type: ignore[return-value]

        # ok=False — classify by status if provided.
        kind = (classify_status(status, value)
                if status is not None else FailureKind.UNKNOWN)
        last_kind = kind
        breaker.record_failure(target)
        if not is_retryable(kind):
            emit("failover.non_retryable", {
                "target": target, "kind": kind.value, "status": status,
            })
            raise FailoverError(
                f"non-retryable {kind.value} status={status} from {target}"
            )

    # All targets exhausted — schedule retry if a kind is configured.
    if task_kind_for_retry and retry_payload is not None:
        try:
            get_retry_queue().enqueue(
                task_kind_for_retry, retry_payload,
                attempt=0, failure_kind=last_kind,
            )
            emit("failover.enqueued_retry", {
                "task_kind": task_kind_for_retry,
                "attempted": attempted,
            })
        except Exception:
            pass
    emit("failover.exhausted", {
        "attempted": attempted, "kind": last_kind.value,
    })
    raise FailoverError(
        f"all {len(attempted)} targets failed (kind={last_kind.value})"
    )
