"""Retry policy — exponential backoff with jitter.

Pure functions:

    delay = min(cap, base × 2^attempt) × (1 ± jitter)

The randomised jitter avoids thundering herds when many callers
retry at the same instant after a global outage.
"""

from __future__ import annotations

import random

from app.resilience.failure_classifier import (
    FailureKind, cooldown_multiplier, is_retryable,
)
from app.resilience.resilience_config import get_config


def compute_delay(attempt: int,
                  *,
                  base_sec: float | None = None,
                  cap_sec: float | None = None,
                  jitter_pct: float | None = None,
                  failure_kind: FailureKind = FailureKind.TRANSIENT) -> float:
    """Return the next-retry delay for the given attempt index (0-based)."""
    cfg = get_config()
    base = base_sec if base_sec is not None else cfg.retry_base_sec
    cap  = cap_sec if cap_sec is not None else cfg.retry_cap_sec
    jit  = jitter_pct if jitter_pct is not None else cfg.retry_jitter_pct
    if attempt < 0:
        attempt = 0
    raw = base * (2 ** attempt) * cooldown_multiplier(failure_kind)
    raw = min(cap, raw)
    if jit > 0:
        delta = raw * jit
        raw = raw + random.uniform(-delta, delta)
    return max(0.0, raw)


def should_retry(attempt: int,
                 failure_kind: FailureKind,
                 *,
                 max_attempts: int | None = None) -> bool:
    cfg = get_config()
    if not is_retryable(failure_kind):
        return False
    cap = max_attempts if max_attempts is not None else cfg.retry_max_attempts
    return attempt < cap


def policy_snapshot() -> dict:
    cfg = get_config()
    return {
        "max_attempts":  cfg.retry_max_attempts,
        "base_sec":      cfg.retry_base_sec,
        "cap_sec":       cfg.retry_cap_sec,
        "jitter_pct":    cfg.retry_jitter_pct,
    }
