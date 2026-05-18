"""Custom exception hierarchy for the resilience package."""

from __future__ import annotations


class ResilienceError(Exception):
    """Base class for every resilience exception."""


class FailureDetectionError(ResilienceError):
    """A failure-detection probe could not be executed."""


class CircuitOpenError(ResilienceError):
    """The breaker for the requested target is open — caller must
    not attempt the call until the cooldown expires."""


class RetryExhaustedError(ResilienceError):
    """All retry attempts (and the persistent queue's TTL) failed."""


class FailoverError(ResilienceError):
    """No healthy alternative was found."""


class DegradedModeBlockedError(ResilienceError):
    """The current degraded-mode level disallows this operation."""


class RecoveryError(ResilienceError):
    """Recovery action failed — caller decides whether to escalate."""
