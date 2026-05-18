"""Route decision — the manager's output.

A RouteDecision summarises:

  * The chosen primary candidate (or None if all rejected).
  * The fallback chain (in priority order).
  * The applied policy + active strategy names.
  * The trace of every strategy's contribution to the final scores.

Returning a single rich object instead of a tuple makes the hot path
self-documenting — admin endpoints can ship the decision JSON straight
to the UI for "explain this routing decision".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from app.routing_strategy.route_candidate import RouteCandidate


@dataclass
class RouteDecision:
    target_node_id: str
    primary:        Optional[RouteCandidate] = None
    fallbacks:      list[RouteCandidate] = field(default_factory=list)
    rejected:       list[RouteCandidate] = field(default_factory=list)
    policy_name:    str = "default"
    strategies:     list[str] = field(default_factory=list)
    started_at:     float = field(default_factory=time.time)
    finished_at:    float = 0.0
    notes:          list[str] = field(default_factory=list)

    @property
    def has_route(self) -> bool:
        return self.primary is not None and not self.primary.rejected

    @property
    def chain(self) -> list[RouteCandidate]:
        out = []
        if self.primary is not None:
            out.append(self.primary)
        out.extend(self.fallbacks)
        return out

    def mark_finished(self) -> None:
        self.finished_at = time.time()

    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000.0, 3)

    def to_dict(self) -> dict:
        return {
            "target_node_id": self.target_node_id,
            "policy_name":    self.policy_name,
            "strategies":     list(self.strategies),
            "primary":        self.primary.to_dict() if self.primary else None,
            "fallbacks":      [c.to_dict() for c in self.fallbacks],
            "rejected":       [c.to_dict() for c in self.rejected],
            "duration_ms":    self.duration_ms(),
            "notes":          list(self.notes),
            "has_route":      self.has_route,
        }
