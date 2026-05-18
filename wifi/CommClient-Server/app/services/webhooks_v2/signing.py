"""
Phase 6 / Module AF — HMAC-SHA256 signing helpers for outbound webhooks.

Signature scheme
----------------
Header ``X-Helen-Signature: sha256=<hex>`` where ``hex`` is::

    hmac_sha256(
        secret,
        f"{timestamp}.{delivery_id}.{event_type}.{body}"
    )

Timestamp tolerance: 5 minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional, Tuple


_TOLERANCE_SEC = 5 * 60


def sign_payload(
    secret: str,
    body: bytes,
    *,
    timestamp: Optional[int] = None,
    delivery_id: str = "",
    event_type: str = "",
) -> Tuple[str, int]:
    """Return ``(header_value, timestamp_used)``."""
    ts = timestamp if timestamp is not None else int(time.time())
    payload = f"{ts}.{delivery_id}.{event_type}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}", ts


def verify_signature(
    secret: str,
    body: bytes,
    *,
    header_value: str,
    timestamp: int,
    delivery_id: str = "",
    event_type: str = "",
    tolerance_sec: int = _TOLERANCE_SEC,
) -> bool:
    if not header_value or "=" not in header_value:
        return False
    if abs(int(time.time()) - int(timestamp)) > tolerance_sec:
        return False
    _, _, given_hex = header_value.partition("=")
    expected, _ = sign_payload(
        secret, body, timestamp=timestamp,
        delivery_id=delivery_id, event_type=event_type,
    )
    expected_hex = expected.split("=", 1)[1]
    return hmac.compare_digest(given_hex, expected_hex)
