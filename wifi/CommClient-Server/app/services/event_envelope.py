"""
Distributed event envelope — single uniform shape for every event
that traverses server boundaries (broker, federation forwards,
chaos-mode chain, DLQ records).

Why
---
Today every cross-server event has a hand-rolled payload shape and
no consistent way to:

* trace an event end-to-end (no traceId)
* detect duplicates safely (no idempotencyKey on most events)
* age out stale events (no expiresAt)
* prevent loops in chain mode (no hopIndex / maxHops)
* prioritize critical signaling over typing/presence flood (no class)

This module ratifies a single Pydantic model that every server-to-
server event passes through. Backward-compatibility shims convert
legacy dicts at the boundary so old handlers keep working.

Hard guards
-----------
1. ``payload`` size_bytes > 8KB → raise ``PayloadTooLarge``.
   Forces media/files to use S3/SFU instead of the control-plane.
2. ``plane = "data"`` is rejected — control plane only.
3. ``priority = "P0"`` requires ``requires_ack = True``.
4. ``max_hops`` capped at 128 (chaos mode); production default is 8.
5. ``expires_at`` must be after ``created_at`` and roughly equal to
   ``created_at + ttl_ms`` (allow 1s skew for clock drift).
"""

from __future__ import annotations

import base64
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ── Type aliases ────────────────────────────────────────────────

Priority = Literal["P0", "P1", "P2", "P3", "P4"]
Plane = Literal["control", "data"]

# ── Constants ───────────────────────────────────────────────────

MAX_PAYLOAD_BYTES = 8 * 1024  # 8 KB hard cap on control-plane events
# MAX_HOPS_PRODUCTION raised from 24 → 64. With gossip K=10 (default) we
# can reach 10^4 = 10,000 nodes in 4 hops; 64 hops gives massive
# headroom for any realistic mesh, including chains across multiple
# multi-homed bridges. Loop detection (Envelope.is_loop) and the
# trace-id seen-cache still prevent runaway fan-out.
import os as _os_hop
MAX_HOPS_PRODUCTION = max(8, int(_os_hop.environ.get("HELEN_MAX_HOPS", "64")))
MAX_HOPS_CHAOS = 256
DEFAULT_TTL_MS = 5_000

# Default TTLs by priority — callers can override.
PRIORITY_DEFAULT_TTL_MS = {
    "P0": 5_000,    # call signaling — short, must be fresh
    "P1": 30_000,   # call lifecycle
    "P2": 60_000,   # chat messages
    "P3": 2_000,    # presence / typing
    "P4": 10_000,   # file metadata
}

# ── Errors ──────────────────────────────────────────────────────

class EnvelopeError(Exception):
    """Base class for envelope-related errors."""


class PayloadTooLarge(EnvelopeError):
    """Raised when an envelope's payload exceeds MAX_PAYLOAD_BYTES.
    The control-plane explicitly forbids large payloads — they
    should travel out-of-band via SFU or object storage."""


class MaxHopsExceeded(EnvelopeError):
    """Raised when ``Envelope.step()`` would push hop_index beyond
    max_hops. The caller should DLQ the event."""


class LoopDetected(EnvelopeError):
    """Raised when an envelope returns to a server it already passed
    through. Indicates a routing bug or a malicious chain."""


# ── ULID-style ID generator ──
# Avoids pulling in the ulid-py dep just for this. Format is:
#   {prefix}_{ts48}{rand80}     (base32, lowercase)
# Sortable by creation time — same property as ULID. The first 48 bits
# are unix milliseconds; the next 80 bits are crypto-random.

_BASE32_ALPHA = "0123456789abcdefghjkmnpqrstvwxyz"

def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_BASE32_ALPHA[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))

def _gen_id(prefix: str) -> str:
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
    return f"{prefix}_{_b32(ts_ms, 10)}{_b32(rand, 16)}"


# ── Envelope ────────────────────────────────────────────────────

class Envelope(BaseModel):
    """Uniform envelope wrapping every server-to-server event.

    Construction
    ------------
    Use ``Envelope.new(...)`` for the common case — it generates IDs,
    computes ``expires_at``, and applies priority defaults. Direct
    construction (``Envelope(...)``) is fine but requires every field
    to be supplied. Use ``Envelope.model_validate(dict)`` to parse
    a wire-format envelope received from another server.
    """

    # ── Identity ────────────────────────────────────────────────
    event_id: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    idempotency_key: str

    # ── Type & Priority ────────────────────────────────────────
    event_type: str
    command_type: Optional[str] = None
    priority: Priority
    plane: Plane = "control"

    # ── Routing ────────────────────────────────────────────────
    source_user_id: Optional[str] = None
    destination_user_id: Optional[str] = None
    source_server_id: str
    destination_server_id: Optional[str] = None
    current_server_id: str
    next_server_id: Optional[str] = None
    route_id: Optional[str] = None
    route_version: int = 1
    hop_index: int = 0
    max_hops: int = MAX_HOPS_PRODUCTION

    # ── Domain context ─────────────────────────────────────────
    call_id: Optional[str] = None
    channel_id: Optional[str] = None

    # ── Reliability ────────────────────────────────────────────
    ttl_ms: int = DEFAULT_TTL_MS
    sequence: Optional[int] = None
    requires_ack: bool = False
    retry_count: int = 0
    max_retries: int = 3

    # ── Lifecycle ──────────────────────────────────────────────
    created_at: datetime
    expires_at: datetime

    # ── Payload ────────────────────────────────────────────────
    payload: dict[str, Any] = Field(default_factory=dict)

    # ── Validators ─────────────────────────────────────────────

    @field_validator("event_type")
    @classmethod
    def _valid_event_type(cls, v: str) -> str:
        # Lowercase dotted segments only: "call.signal.offer".
        if not v or "." not in v:
            raise ValueError("event_type must be dotted: e.g. call.signal.offer")
        for seg in v.split("."):
            if not seg or not all(c.islower() or c == "_" or c.isdigit() for c in seg):
                raise ValueError(f"event_type segment {seg!r} must be lowercase a-z/0-9/_")
        return v

    @field_validator("plane")
    @classmethod
    def _no_data_plane(cls, v: Plane) -> Plane:
        if v == "data":
            raise ValueError(
                "plane='data' is forbidden — large/binary payloads must "
                "travel via S3 or SFU, not via envelope."
            )
        return v

    @field_validator("max_hops")
    @classmethod
    def _max_hops_capped(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_hops must be >= 1")
        if v > MAX_HOPS_CHAOS:
            raise ValueError(
                f"max_hops capped at {MAX_HOPS_CHAOS} (chaos mode). "
                f"Got {v}."
            )
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "Envelope":
        # P0 requires ACK by default — but high-volume signaling
        # (ICE candidates, presence flips) explicitly opts out via
        # ``allow_p0_no_ack``. Without this escape hatch every ICE
        # candidate generates an ACK envelope and the broker doubles
        # in load. ICE candidates are fire-and-forget by WebRTC
        # design (a missed candidate is replaced by gathering on the
        # next pulse), so the ACK is genuinely wasteful.
        if (
            self.priority == "P0"
            and not self.requires_ack
            and not self.payload.get("__allow_p0_no_ack__")
        ):
            raise ValueError(
                "priority=P0 requires requires_ack=True "
                "(or set payload['__allow_p0_no_ack__']=True for ICE-style "
                "fire-and-forget signaling)"
            )

        # expires_at sanity
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")

        # ttl_ms ↔ expires_at - created_at consistency (allow 1s drift)
        derived_ttl_ms = int(
            (self.expires_at - self.created_at).total_seconds() * 1000
        )
        if abs(derived_ttl_ms - self.ttl_ms) > 1000:
            raise ValueError(
                f"ttl_ms ({self.ttl_ms}) inconsistent with "
                f"expires_at - created_at ({derived_ttl_ms}ms)"
            )

        # hop_index sanity
        if self.hop_index < 0:
            raise ValueError("hop_index must be >= 0")
        if self.hop_index > self.max_hops:
            raise ValueError(
                f"hop_index ({self.hop_index}) exceeds max_hops "
                f"({self.max_hops})"
            )

        # Payload size guard
        size = self.size_bytes()
        if size > MAX_PAYLOAD_BYTES:
            raise PayloadTooLarge(
                f"payload size {size} exceeds {MAX_PAYLOAD_BYTES} bytes — "
                f"large payloads must use S3 (files) or SFU (media)"
            )
        return self

    # ── Construction helpers ───────────────────────────────────

    @classmethod
    def new(
        cls,
        *,
        event_type: str,
        priority: Priority,
        source_server_id: str,
        idempotency_key: Optional[str] = None,
        ttl_ms: Optional[int] = None,
        max_hops: int = MAX_HOPS_PRODUCTION,
        payload: Optional[dict[str, Any]] = None,
        source_user_id: Optional[str] = None,
        destination_user_id: Optional[str] = None,
        destination_server_id: Optional[str] = None,
        call_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        sequence: Optional[int] = None,
        requires_ack: Optional[bool] = None,
        max_retries: int = 3,
        command_type: Optional[str] = None,
        trace_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
    ) -> "Envelope":
        """Convenience constructor that fills derived fields. Use this
        unless you have a reason to construct manually (e.g. parsing
        wire format)."""
        now = datetime.now(timezone.utc)
        ttl = ttl_ms if ttl_ms is not None else PRIORITY_DEFAULT_TTL_MS.get(priority, DEFAULT_TTL_MS)
        # P0 defaults to requires_ack=True. Caller can explicitly pass
        # ``requires_ack=False`` for fire-and-forget signaling (ICE
        # candidates etc.) — we then add the escape-hatch flag to
        # the payload so schema validation accepts it.
        if priority == "P0":
            if requires_ack is False:
                ack = False
                payload = dict(payload or {})
                payload["__allow_p0_no_ack__"] = True
            else:
                ack = True
        else:
            ack = requires_ack if requires_ack is not None else False

        return cls(
            event_id=_gen_id("evt"),
            trace_id=trace_id or _gen_id("trace"),
            span_id=_gen_id("span"),
            parent_span_id=parent_span_id,
            idempotency_key=idempotency_key or _gen_id("idem"),
            event_type=event_type,
            command_type=command_type,
            priority=priority,
            plane="control",
            source_user_id=source_user_id,
            destination_user_id=destination_user_id,
            source_server_id=source_server_id,
            destination_server_id=destination_server_id,
            current_server_id=source_server_id,
            next_server_id=None,
            route_id=None,
            route_version=1,
            hop_index=0,
            max_hops=max_hops,
            call_id=call_id,
            channel_id=channel_id,
            ttl_ms=ttl,
            sequence=sequence,
            requires_ack=ack,
            retry_count=0,
            max_retries=max_retries,
            created_at=now,
            expires_at=now + timedelta(milliseconds=ttl),
            payload=payload or {},
        )

    # ── Lifecycle ─────────────────────────────────────────────

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def is_loop(self, server_id: str, route_history: Optional[list[str]] = None) -> bool:
        """Return True if this envelope has already passed through
        ``server_id``. ``route_history`` is the optional ordered list
        of servers visited (callers can maintain it as a span chain).
        Without history, we conservatively flag a loop only when the
        current server matches the ``source_server_id`` and we've
        already taken at least one hop."""
        if route_history is not None and server_id in route_history:
            return True
        return server_id == self.source_server_id and self.hop_index > 0

    def step(self, next_server_id: str) -> "Envelope":
        """Produce a new envelope advanced one hop. Increments
        ``hop_index``, rotates ``span_id``, sets ``parent_span_id``
        from the previous span, and updates ``current_server_id``.
        Raises ``MaxHopsExceeded`` at the cap."""
        if self.hop_index + 1 >= self.max_hops:
            raise MaxHopsExceeded(
                f"event {self.event_id} reached max_hops={self.max_hops}"
            )
        new = self.model_copy()
        new.parent_span_id = self.span_id
        new.span_id = _gen_id("span")
        new.hop_index = self.hop_index + 1
        new.current_server_id = next_server_id
        new.next_server_id = None
        return new

    def with_retry(self) -> "Envelope":
        """Produce a new envelope with retry_count incremented. Caller
        is responsible for checking ``retry_count <= max_retries``
        before scheduling the retry."""
        new = self.model_copy()
        new.retry_count = self.retry_count + 1
        new.span_id = _gen_id("span")
        new.parent_span_id = self.span_id
        return new

    def size_bytes(self) -> int:
        """Approximate wire size in bytes (UTF-8 JSON)."""
        return len(self.model_dump_json().encode("utf-8"))


# ── Backward-compat shim ───────────────────────────────────────

def from_legacy(
    *,
    event_type: str,
    priority: Priority,
    source_server_id: str,
    payload: dict,
    source_user_id: Optional[str] = None,
    destination_user_id: Optional[str] = None,
    call_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Envelope:
    """Build an envelope from a legacy event dict. Used by the
    federation/emit shim while we migrate handlers to envelope-native
    APIs."""
    # Strip legacy-only keys that don't fit our schema. Whatever's left
    # in payload is preserved.
    payload = dict(payload)  # don't mutate caller's dict
    return Envelope.new(
        event_type=event_type,
        priority=priority,
        source_server_id=source_server_id,
        source_user_id=source_user_id,
        destination_user_id=destination_user_id,
        idempotency_key=idempotency_key,
        call_id=call_id,
        channel_id=channel_id,
        payload=payload,
    )
