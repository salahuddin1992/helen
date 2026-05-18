"""Route candidate — one option for delivering a request.

A candidate wraps a concrete ``Route`` (from the multipath_router
package) plus per-strategy metadata. Strategies *annotate* it with
their score contribution + reasoning so the final ``RouteDecision``
can carry an explainable trace.

The candidate is a plain dataclass — no I/O, no mutations beyond the
explicit setter methods.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RouteCandidate:
    """One delivery option, scored and annotated."""
    # The underlying concrete route — kept generic so we don't import
    # the heavy multipath_router types at strategy-package import time.
    route:          Any                                  # services.multipath_router.Route
    candidate_id:   str = ""                             # cosmetic ID for traces
    weight:         float = 0.0                          # final composed score
    rejected:       bool = False
    rejection_reason: Optional[str] = None
    contributions:  dict = field(default_factory=dict)   # strategy_name → score
    annotations:    dict = field(default_factory=dict)
    created_at:     float = field(default_factory=time.time)

    @property
    def route_type(self) -> str:
        # Defensive — any route object with a ``route_type`` attr works.
        rt = getattr(self.route, "route_type", None)
        return getattr(rt, "value", str(rt)) if rt is not None else "unknown"

    @property
    def first_hop(self) -> str:
        hops = getattr(self.route, "hops", None) or []
        return hops[0] if hops else ""

    @property
    def hop_count(self) -> int:
        hops = getattr(self.route, "hops", None) or []
        return len(hops)

    def reject(self, reason: str) -> None:
        self.rejected = True
        self.rejection_reason = reason
        self.weight = 0.0

    def add_contribution(self, strategy_name: str, score: float) -> None:
        self.contributions[strategy_name] = round(float(score), 6)

    def annotate(self, key: str, value: Any) -> None:
        self.annotations[key] = value

    def to_dict(self) -> dict:
        return {
            "candidate_id":       self.candidate_id,
            "route_type":         self.route_type,
            "first_hop":          self.first_hop,
            "hop_count":          self.hop_count,
            "weight":             round(self.weight, 6),
            "rejected":           self.rejected,
            "rejection_reason":   self.rejection_reason,
            "contributions":      dict(self.contributions),
            "annotations":        dict(self.annotations),
        }
