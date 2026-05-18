"""Routing policy — declares which strategies apply for a request.

A ``RoutingPolicy`` is essentially a recipe: a name + an ordered
list of strategies + per-strategy weight overrides. The manager
asks the policy for its strategy list, then runs them in turn.

Policies are values (no behaviour) so they can be swapped at
runtime without restarting; the default policy adapts based on
cluster conditions (degraded → bias toward proxy, partition →
add federation, etc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoutingPolicy:
    name:                 str = "default"
    strategy_names:       list[str] = field(default_factory=lambda: [
        # Order matters: trust + nat-aware first to filter, scoring
        # last to assign weights.
        "trust_aware",
        "nat_aware",
        "relay",
        "proxy",
        "federation",
        "load_balancing",
        "multipath",
        "failover",
        "adaptive",
    ])
    weight_overrides:     dict = field(default_factory=dict)
    require_trusted_hop:  bool = True
    enable_failover:      bool = True
    enable_multipath:     bool = True
    notes:                list[str] = field(default_factory=list)

    def with_strategies(self, strategies: list[str]) -> "RoutingPolicy":
        """Return a copy with a different strategy list."""
        return RoutingPolicy(
            name=self.name,
            strategy_names=list(strategies),
            weight_overrides=dict(self.weight_overrides),
            require_trusted_hop=self.require_trusted_hop,
            enable_failover=self.enable_failover,
            enable_multipath=self.enable_multipath,
            notes=list(self.notes),
        )

    def to_dict(self) -> dict:
        return {
            "name":                 self.name,
            "strategy_names":       list(self.strategy_names),
            "weight_overrides":     dict(self.weight_overrides),
            "require_trusted_hop":  self.require_trusted_hop,
            "enable_failover":      self.enable_failover,
            "enable_multipath":     self.enable_multipath,
            "notes":                list(self.notes),
        }


# ── Built-in policies ───────────────────────────────────────────


def policy_default() -> RoutingPolicy:
    return RoutingPolicy(name="default")


def policy_lan_only() -> RoutingPolicy:
    """LAN-trusted environment: skip federation/tunnel strategies."""
    return RoutingPolicy(
        name="lan_only",
        strategy_names=[
            "trust_aware", "relay", "proxy",
            "load_balancing", "multipath", "failover",
        ],
    )


def policy_partition_recovery() -> RoutingPolicy:
    """Active during a detected partition — prefer federation +
    bridge routes that may reach the other side."""
    return RoutingPolicy(
        name="partition_recovery",
        strategy_names=[
            "trust_aware", "nat_aware", "federation",
            "relay", "proxy", "multipath", "failover",
        ],
        notes=["partition_active"],
    )


def policy_high_priority() -> RoutingPolicy:
    """Latency-sensitive — bias scoring weights and skip slow
    classes."""
    return RoutingPolicy(
        name="high_priority",
        strategy_names=[
            "trust_aware", "load_balancing", "multipath", "failover",
        ],
        weight_overrides={"w_latency": 0.45, "w_hops": 0.20},
    )


def policy_for_context(*, is_majority: bool, backpressure_level: str,
                       rendezvous_available: bool) -> RoutingPolicy:
    """Pick the right built-in policy for the current state."""
    if not is_majority:
        return policy_partition_recovery()
    if backpressure_level == "rejected":
        return policy_high_priority()
    return policy_default()
