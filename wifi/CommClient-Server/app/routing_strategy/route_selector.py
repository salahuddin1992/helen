"""Route selector — picks primary + fallback chain from scored candidates.

The scoring engine assigns weights; the selector applies tiebreakers
and returns the final ordering used by the manager.

Tiebreaker priority (highest weight first, then):
  1. Lower hop_count.
  2. Higher class floor.
  3. Lexicographic first_hop (deterministic).
"""

from __future__ import annotations

from typing import Iterable

from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_scoring_engine import _CLASS_FLOOR
from app.routing_strategy.strategy_exceptions import AllRoutesRejectedError


def _tiebreak_key(c: RouteCandidate) -> tuple:
    return (
        -c.weight,
        c.hop_count,
        -_CLASS_FLOOR.get(c.route_type, 0.5),
        c.first_hop or "",
    )


def select_top_k(
    candidates: Iterable[RouteCandidate],
    k: int = 4,
) -> list[RouteCandidate]:
    """Return the top-K candidates ordered best-first.

    Rejected candidates are dropped; if every candidate is rejected
    the function raises AllRoutesRejectedError so the manager can
    short-circuit with a clean error.
    """
    survivors = [c for c in candidates if not c.rejected and c.weight > 0]
    if not survivors:
        raise AllRoutesRejectedError(
            f"all {sum(1 for _ in candidates)} candidates rejected"
        )
    survivors.sort(key=_tiebreak_key)
    return survivors[: max(1, int(k))]


def split_primary_and_fallbacks(
    ordered: list[RouteCandidate],
) -> tuple[RouteCandidate, list[RouteCandidate]]:
    """Convenience helper: peels off the primary, returns
    (primary, fallback_list)."""
    if not ordered:
        raise AllRoutesRejectedError("empty selection")
    return ordered[0], list(ordered[1:])
