"""Custom exception types for the routing strategy package.

Centralised so callers can catch a single base class
(``StrategyError``) and so the manager can map exception types to
specific RouteDecision outcomes (e.g. NoCandidatesError → 404).
"""

from __future__ import annotations


class StrategyError(Exception):
    """Base class for every strategy-package exception."""


class NoCandidatesError(StrategyError):
    """Raised when route discovery returned zero candidates."""


class AllRoutesRejectedError(StrategyError):
    """Raised when every candidate scored 0 (all blocked or in cooldown)."""


class StrategyConfigError(StrategyError):
    """Raised when ``strategy_config`` is malformed or the requested
    strategy mode is unknown."""


class RouteSelectionError(StrategyError):
    """Raised when the selector cannot reach a final pick (ties on a
    deterministic tiebreaker, etc.)."""


class StrategyTimeoutError(StrategyError):
    """Raised when a strategy hook exceeds its allotted budget."""
