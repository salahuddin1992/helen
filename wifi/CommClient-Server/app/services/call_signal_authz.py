"""
CallSignalAuthz — lightweight per-server shadow of cross-server call
participation, used SOLELY to authorize WebRTC signal relay (offer /
answer / ice_candidate / call_signal) when ActiveCall state lives on a
DIFFERENT Helen server in the federation.

Why this exists
---------------
Each Helen server owns its own ``ActiveCall`` registry in
``call_service``. When user A on server-1 calls user B on server-2,
server-1 holds the authoritative call state; server-2 does not. The
existing security check inside the signal handlers
(``call_service.get_user_call(user_id)``) therefore fails on server-2
and B's outbound signals get dropped as ``unauthorized``.

The shadow plugs that gap without trying to mirror full call state
across the federation. We only record the minimum needed to *authorize*:
  * the call_id
  * the set of user_ids that are *allowed* to signal each other within it

When a federated event (``call_incoming``, ``call_accepted``,
``call_peer_joined``, etc.) arrives at a peer, the federation receive
handler seeds / extends this shadow before re-emitting to local sids.
When the call ends (``call_hangup``, ``call_peer_left`` clearing the
last participant), the shadow is cleared.

This is intentionally NOT a substitute for full federated call state —
mute toggles, host promotion, mediasoup router allocation, idempotency
caches etc. are all still server-local. The shadow is read-only from
the signal-handler's perspective: it answers exactly one question:
*are these two users allowed to send WebRTC signals to each other right
now?*
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)

# Default TTL for a shadow entry — generous (calls can run for hours)
# but capped so a missed teardown event doesn't leave permanent grants.
_DEFAULT_TTL_SECONDS = 3 * 60 * 60  # 3h
# Hard cap on shadows we hold simultaneously, in case of runaway leaks.
_MAX_SHADOWS = 10_000


@dataclass
class _Shadow:
    participants: set[str] = field(default_factory=set)
    expires_at: float = 0.0
    # The server_id of the Helen instance that holds the *authoritative*
    # ActiveCall entry for this call_id. Set when a server learns about
    # the call (locally on initiate, or via /api/federation/emit). Used
    # by lifecycle RPC forwarding (accept/reject/leave/hangup) so a
    # callee on a sibling server can forward its action back to the
    # owning server instead of failing with "call_not_found".
    origin_server_id: str | None = None


class CallSignalAuthz:
    """Thread-safe registry: call_id -> {participants, expires_at}.

    All public methods are O(1). Lock contention is negligible because
    the calls are short and the registry sees one write per signaling
    lifecycle event (a few per call), not per WebRTC packet.

    Distributed-transformation note
    -------------------------------
    The shadow is currently process-local. For full multi-server
    safety (where a federated_emit on server-A seeds the shadow on
    server-A but server-B handles the next signal), we need a Redis-
    backed implementation that mirrors mutations across the cluster.
    See app/services/distributed_lock_service.py for the lock
    primitive; the shadow store itself needs migration to Redis hash:
    ``helen:authz:call:{call_id}:participants`` with TTL renewal.
    Tracked as Phase 1 follow-up — not implemented in this batch
    because it requires a careful migration of every seed/extend/clear
    call site to async, plus a fallback for the LAN-only deployment
    path.
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS):
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        self._shadows: dict[str, _Shadow] = {}

    # ── mutation ────────────────────────────────────────────

    def seed(
        self,
        call_id: str,
        participants: list[str] | set[str],
        origin_server_id: str | None = None,
    ) -> None:
        """Record the initial participant set for a call.

        ``origin_server_id`` is the Helen server that holds the
        authoritative ActiveCall. Pass our own server_id when seeding
        from a local lifecycle handler; pass the upstream peer's id
        when seeding from a federated event.
        """
        if not call_id:
            return
        with self._lock:
            self._gc_locked()
            sh = self._shadows.get(call_id)
            if sh is None:
                sh = _Shadow()
                self._shadows[call_id] = sh
            sh.participants.update(p for p in participants if p)
            sh.expires_at = time.time() + self._ttl
            # Origin can be set lazily — first writer wins, but we never
            # overwrite a known origin with None (defensive against an
            # event that arrived without the header).
            if origin_server_id and not sh.origin_server_id:
                sh.origin_server_id = origin_server_id
            if len(self._shadows) > _MAX_SHADOWS:
                # LRU-ish eviction — drop the oldest by expiry. We keep
                # the cap soft (just log) because real production sees
                # nowhere near this volume of concurrent calls.
                logger.warning(
                    "call_signal_authz_shadow_count_high",
                    count=len(self._shadows),
                )

    def add_participant(self, call_id: str, user_id: str) -> None:
        if not call_id or not user_id:
            return
        with self._lock:
            sh = self._shadows.get(call_id)
            if sh is None:
                # Late add (peer_joined arrived before incoming on a
                # delayed federation hop). Auto-create with just this
                # user — the next event will fill in the rest.
                sh = _Shadow()
                self._shadows[call_id] = sh
            sh.participants.add(user_id)
            sh.expires_at = time.time() + self._ttl

    def remove_participant(self, call_id: str, user_id: str) -> None:
        if not call_id or not user_id:
            return
        with self._lock:
            sh = self._shadows.get(call_id)
            if sh is None:
                return
            sh.participants.discard(user_id)
            if not sh.participants:
                self._shadows.pop(call_id, None)

    def clear(self, call_id: str) -> None:
        if not call_id:
            return
        with self._lock:
            self._shadows.pop(call_id, None)

    # ── inspection ──────────────────────────────────────────

    def is_authorized(self, call_id: str, sender_id: str, target_id: str) -> bool:
        """True when both peers are in the recorded participant set
        AND the entry is not expired. Conservative: any missing field
        returns False so the security check stays strict."""
        if not call_id or not sender_id or not target_id:
            return False
        if sender_id == target_id:
            return False  # self-signaling never makes sense
        with self._lock:
            sh = self._shadows.get(call_id)
            if sh is None:
                return False
            if sh.expires_at < time.time():
                self._shadows.pop(call_id, None)
                return False
            return sender_id in sh.participants and target_id in sh.participants

    def participants(self, call_id: str) -> set[str]:
        """Return a *copy* of the current participant set (or empty set)."""
        with self._lock:
            sh = self._shadows.get(call_id)
            if sh is None or sh.expires_at < time.time():
                return set()
            return set(sh.participants)

    def origin_of(self, call_id: str) -> str | None:
        """Return the origin server_id that owns this call's ActiveCall,
        or None if unknown / expired. Used by RPC-forward path."""
        with self._lock:
            sh = self._shadows.get(call_id)
            if sh is None or sh.expires_at < time.time():
                return None
            return sh.origin_server_id

    def size(self) -> int:
        with self._lock:
            return len(self._shadows)

    # ── housekeeping ────────────────────────────────────────

    def _gc_locked(self) -> None:
        """Drop expired entries. Called inside _lock by mutators."""
        now = time.time()
        expired = [cid for cid, sh in self._shadows.items() if sh.expires_at < now]
        for cid in expired:
            self._shadows.pop(cid, None)


# Module-level singleton — the rest of the codebase imports this.
call_signal_authz = CallSignalAuthz()


# ── Federation hook helpers ─────────────────────────────────
#
# These map the wire-level event names that fly through
# /api/federation/emit to the right shadow operation. Centralized here
# so the federation receive handler stays small and the mapping is
# documented in one place.

# Lifecycle events — each one tells us something about who is allowed
# to signal whom.
_SEED_EVENTS = {
    # 1-to-1
    "call:incoming",      "call_incoming",
    "call:accepted",      "call_accepted",
    "call:peer_ready",
    # group
    "call:peer_joined",   "call_participant_joined",
    "call:group_ringing",
}
_ADD_EVENTS = {
    "call:peer_joined",   "call_participant_joined",
}
_REMOVE_EVENTS = {
    "call:peer_left",     "call_participant_left",
}
_CLEAR_EVENTS = {
    "call:ended",         "call_hangup", "call_ended",
}


def apply_federation_event(
    event: str,
    payload: dict,
    origin_server_id: str | None = None,
) -> None:
    """Update the shadow based on an event arriving via federation.

    Safe to call for every event — non-call events are no-ops.

    ``origin_server_id`` should be the immediate sender's server_id
    (typically taken from the ``X-Federation-Origin`` request header).
    For call events the immediate sender is the call's origin server,
    so we record it here and use it later for RPC forwarding.
    """
    if not event or not isinstance(payload, dict):
        return
    call_id = payload.get("call_id")
    if not call_id or not isinstance(call_id, str):
        return

    if event in _CLEAR_EVENTS:
        call_signal_authz.clear(call_id)
        return

    if event in _REMOVE_EVENTS:
        uid = payload.get("user_id")
        if isinstance(uid, str):
            call_signal_authz.remove_participant(call_id, uid)
        return

    if event in _SEED_EVENTS:
        # Build the participant set from whatever fields the event carries.
        seed: set[str] = set()
        for key in ("caller_id", "callee_id", "user_id", "from_id", "ended_by"):
            v = payload.get(key)
            if isinstance(v, str):
                seed.add(v)
        # Some events carry the full list explicitly.
        plist = payload.get("participants")
        if isinstance(plist, list):
            for p in plist:
                if isinstance(p, str):
                    seed.add(p)
                elif isinstance(p, dict):
                    pid = p.get("user_id") or p.get("id")
                    if isinstance(pid, str):
                        seed.add(pid)
        if seed:
            call_signal_authz.seed(
                call_id, seed, origin_server_id=origin_server_id,
            )
        if event in _ADD_EVENTS:
            uid = payload.get("user_id")
            if isinstance(uid, str):
                call_signal_authz.add_participant(call_id, uid)
