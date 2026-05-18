"""Failure classifier — decides what *kind* of failure occurred.

Different failure modes need different remediation:

  * TRANSIENT  — try again with backoff (network blip, GC pause).
  * PERMANENT  — stop retrying, ask the operator (auth refused).
  * NETWORK    — try a different path (peer/route alternation).
  * SECURITY   — block the peer (HMAC fail, replay attempt).
  * OVERLOAD   — slow down (backpressure, 429).
  * UNKNOWN    — default; treated like TRANSIENT.

Classification is based on:

  * exception type  — TimeoutError → TRANSIENT, PermissionError → SECURITY
  * status code     — 5xx → TRANSIENT, 401/403 → SECURITY, 429 → OVERLOAD
  * response detail — "peer_blocked" → SECURITY, etc.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    NETWORK   = "network"
    SECURITY  = "security"
    OVERLOAD  = "overload"
    UNKNOWN   = "unknown"


_TRANSIENT_EXCEPTIONS = (
    "TimeoutError", "asyncio.TimeoutError",
    "ConnectionResetError", "ConnectionAbortedError",
    "BrokenPipeError",
)

_NETWORK_EXCEPTIONS = (
    "ConnectionError", "ConnectionRefusedError",
    "OSError", "socket.gaierror", "RemoteDisconnected",
)

_SECURITY_EXCEPTIONS = (
    "PermissionError", "PeerHandshakeError",
    "PeerQuarantinedError",
)


def classify_exception(exc: BaseException) -> FailureKind:
    name = type(exc).__name__
    if name in _TRANSIENT_EXCEPTIONS:
        return FailureKind.TRANSIENT
    if name in _NETWORK_EXCEPTIONS:
        return FailureKind.NETWORK
    if name in _SECURITY_EXCEPTIONS:
        return FailureKind.SECURITY
    return FailureKind.UNKNOWN


def classify_status(status_code: int, body: Any = None) -> FailureKind:
    """Classify by HTTP status + optional body for detail strings."""
    if 500 <= status_code <= 599:
        return FailureKind.TRANSIENT
    if status_code == 429:
        return FailureKind.OVERLOAD
    if status_code in (401,):
        return FailureKind.SECURITY
    if status_code == 403:
        # 403 usually = permission, but sometimes peer_blocked which
        # is also security.
        if isinstance(body, dict):
            detail = (body.get("detail") or "").lower()
            if "blocked" in detail or "quarantine" in detail:
                return FailureKind.SECURITY
        return FailureKind.SECURITY
    if status_code == 404:
        return FailureKind.PERMANENT
    if status_code == 502 or status_code == 503 or status_code == 504:
        return FailureKind.NETWORK
    if 200 <= status_code < 400:
        return FailureKind.UNKNOWN  # not a failure
    return FailureKind.UNKNOWN


def is_retryable(kind: FailureKind) -> bool:
    """True iff retrying this kind has a chance of succeeding."""
    return kind in (
        FailureKind.TRANSIENT,
        FailureKind.NETWORK,
        FailureKind.OVERLOAD,
        FailureKind.UNKNOWN,
    )


def cooldown_multiplier(kind: FailureKind) -> float:
    """How aggressively to back off given the failure kind."""
    return {
        FailureKind.TRANSIENT: 1.0,
        FailureKind.NETWORK:   1.5,
        FailureKind.OVERLOAD:  2.0,
        FailureKind.UNKNOWN:   1.0,
        FailureKind.PERMANENT: 0.0,   # don't retry
        FailureKind.SECURITY:  0.0,   # don't retry
    }.get(kind, 1.0)
