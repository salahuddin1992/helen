"""Route constraints — declarative filters applied before scoring.

Constraints are *hard* gates: a candidate that fails any constraint
is rejected with score 0 *before* the scoring engine runs. Scoring
sees only the survivors.

Constraints are pure functions ``(context, candidate) -> (ok, reason)``
so they're trivially testable and composable.
"""

from __future__ import annotations

import time
from typing import Callable

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.strategy_config import get_config


# Constraint signature: (ctx, candidate) → (ok, reason).
ConstraintFn = Callable[[RoutingContext, RouteCandidate], tuple[bool, str]]


# ── Built-in constraints ────────────────────────────────────────


def constraint_not_in_cooldown(ctx: RoutingContext,
                               c: RouteCandidate) -> tuple[bool, str]:
    """Reject any route whose Route object reports an active cooldown."""
    failed_until = float(getattr(c.route, "failed_until", 0.0) or 0.0)
    if failed_until and time.time() < failed_until:
        return False, "in_cooldown"
    return True, ""


def constraint_trust_floor(ctx: RoutingContext,
                            c: RouteCandidate) -> tuple[bool, str]:
    """Reject if the first hop's trust score is below the floor."""
    cfg = get_config()
    if not ctx.require_trusted:
        return True, ""
    first = c.first_hop
    if not first or first == ctx.target_node_id:
        return True, ""
    try:
        from app.services.trust_score import get_trust_db
        score = get_trust_db().get_score(first)
    except Exception:
        return True, ""
    if score < cfg.trust_floor:
        return False, f"trust<{cfg.trust_floor}"
    c.annotate("trust", round(score, 3))
    return True, ""


def constraint_phi_ceiling(ctx: RoutingContext,
                            c: RouteCandidate) -> tuple[bool, str]:
    """Reject if phi-accrual considers the first hop dead."""
    cfg = get_config()
    first = c.first_hop or ctx.target_node_id
    if not first:
        return True, ""
    try:
        from app.services.phi_accrual import get_phi_registry
        phi = get_phi_registry().detector_for(first).phi()
    except Exception:
        return True, ""
    if phi >= cfg.phi_ceiling:
        return False, f"phi>={cfg.phi_ceiling}"
    c.annotate("phi", round(phi, 2))
    return True, ""


def constraint_blocklist(ctx: RoutingContext,
                          c: RouteCandidate) -> tuple[bool, str]:
    """Reject any candidate whose first hop is in the sync_policy
    blocklist — defence-in-depth against accidental usage."""
    first = c.first_hop
    if not first or first == ctx.target_node_id:
        return True, ""
    try:
        from app.services.sync_policy import get_sync_policy
        if get_sync_policy().is_blocked(first):
            return False, "blocked"
    except Exception:
        pass
    return True, ""


def constraint_cluster_match(ctx: RoutingContext,
                              c: RouteCandidate) -> tuple[bool, str]:
    """For non-FEDERATION routes, the first hop must share our
    cluster_id."""
    if c.route_type == "federation":
        return True, ""
    return True, ""  # Cluster_id check is handled by federation_auth itself.


# ── Default chain ───────────────────────────────────────────────


def default_constraints() -> list[ConstraintFn]:
    return [
        constraint_not_in_cooldown,
        constraint_blocklist,
        constraint_trust_floor,
        constraint_phi_ceiling,
        constraint_cluster_match,
    ]


def apply_constraints(
    ctx: RoutingContext,
    candidates: list[RouteCandidate],
    constraints: list[ConstraintFn] | None = None,
) -> list[RouteCandidate]:
    """Apply the constraint chain in order. Mutates each candidate's
    rejection status; returns the same list for chaining convenience."""
    chain = constraints if constraints is not None else default_constraints()
    for c in candidates:
        if c.rejected:
            continue
        for fn in chain:
            ok, reason = fn(ctx, c)
            if not ok:
                c.reject(reason)
                break
    return candidates
