"""Routing context — the per-request input bag.

Strategies read the context to decide what to do. The context is
*built* by ``RoutingStrategyManager`` on every ``route()`` call and
is *immutable* during strategy evaluation (strategies write to
candidates / decision, never to the context).

Anything strategies need to know about the current request, the
caller, the cluster state, and the operator policy lives here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class RoutingContext:
    """Immutable per-request context."""
    # Target
    target_node_id: str
    method:         str = "GET"
    path:           str = "/"
    body:           Any = None
    headers:        Optional[dict] = None

    # Caller intent
    essential:      bool = False           # bypass backpressure?
    require_trusted: bool = True
    max_attempts:   int = 3
    deadline_sec:   float = 5.0
    parallel:       int = 1                # parallel route fanout

    # Cluster snapshot (filled by the manager)
    self_node_id:   str = ""
    cluster_id:     str = "default"
    is_majority:    bool = True
    backpressure_level: str = "normal"     # normal/degraded/rejected
    rendezvous_available: bool = False

    # Trace
    request_id:     str = ""
    started_at:     float = field(default_factory=time.time)

    def age_sec(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def has_deadline_remaining(self) -> bool:
        return self.age_sec() < self.deadline_sec

    def remaining_budget_sec(self) -> float:
        return max(0.0, self.deadline_sec - self.age_sec())

    def to_dict(self) -> dict:
        return {
            "target_node_id":  self.target_node_id,
            "method":          self.method,
            "path":            self.path,
            "essential":       self.essential,
            "require_trusted": self.require_trusted,
            "max_attempts":    self.max_attempts,
            "deadline_sec":    self.deadline_sec,
            "parallel":        self.parallel,
            "self_node_id":    self.self_node_id,
            "cluster_id":      self.cluster_id,
            "is_majority":     self.is_majority,
            "backpressure_level": self.backpressure_level,
            "rendezvous_available": self.rendezvous_available,
            "request_id":      self.request_id,
            "age_sec":         round(self.age_sec(), 4),
        }
