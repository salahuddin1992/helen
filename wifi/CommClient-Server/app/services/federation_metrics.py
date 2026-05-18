"""
Process-local federation metrics.

Counters are cheap atomic ints we bump at hot-path interesting points
(HMAC verification, relay alloc/release, circuit-breaker trips). Read
once per scrape via `snapshot()`. Nothing is persisted — restart wipes
them, which is what you want for a diagnostic pane.

Wired from:
  * `app.core.federation_auth.verify_request` — hmac_ok / hmac_fail by reason
  * `app.services.federation_service` — breaker open/close
  * `app.api.routes.federation` (relay alloc/release paths)
  * `app.services.relay_worker` — janitor reaps
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _PeerCounters:
    """Per-peer bridge traffic. All values are monotonically increasing
    except ``last_activity_at`` which tracks the latest touch."""
    emits_sent: int = 0        # federation emits we initiated toward this peer
    emits_received: int = 0    # federation emits this peer initiated to us
    forwards_attempted: int = 0  # chain-routed forwards we sent via this peer
    forwards_incoming: int = 0   # chain-routed forwards we received from this peer
    dedup_drops: int = 0       # duplicate message_ids we dropped from this peer
    bytes_out: int = 0         # bytes we sent over federation (body size)
    bytes_in: int = 0          # bytes we received over federation
    ok_responses: int = 0      # 2xx from this peer
    error_responses: int = 0   # non-2xx or exceptions from this peer
    last_activity_at: float = 0.0


@dataclass
class _Counters:
    # HMAC auth
    hmac_verified_ok: int = 0
    hmac_verified_fail: dict[str, int] = field(default_factory=dict)  # reason → count
    # Relay control plane
    relay_alloc_ok: int = 0
    relay_alloc_quota_denied: int = 0
    relay_alloc_rate_limited: int = 0
    relay_released: int = 0
    relay_janitor_reaped: int = 0
    # Chain builder
    relay_chains_built: int = 0
    relay_chains_failed: int = 0
    # Presence
    presence_pushes_received: int = 0
    presence_pushes_sent: int = 0
    # Per-peer bridge counters
    per_peer: dict[str, _PeerCounters] = field(default_factory=dict)
    # Rolling event log for live admin dashboard
    events: deque = field(default_factory=lambda: deque(maxlen=200))
    # Started timestamp for uptime calc
    started_at: float = field(default_factory=time.time)


_counters = _Counters()
_events_lock = threading.Lock()


def incr(name: str, n: int = 1) -> None:
    v = getattr(_counters, name, None)
    if isinstance(v, int):
        setattr(_counters, name, v + n)


def incr_hmac_fail(reason: str) -> None:
    _counters.hmac_verified_fail[reason] = (
        _counters.hmac_verified_fail.get(reason, 0) + 1
    )


def _peer(peer_id: str) -> _PeerCounters:
    """Get-or-create the per-peer bucket. Not locked — the atomic int
    increments underneath are good enough for dashboard-grade accuracy."""
    bucket = _counters.per_peer.get(peer_id)
    if bucket is None:
        bucket = _PeerCounters()
        _counters.per_peer[peer_id] = bucket
    return bucket


def bump_peer(peer_id: str, *,
              emits_sent: int = 0, emits_received: int = 0,
              forwards_attempted: int = 0, forwards_incoming: int = 0,
              dedup_drops: int = 0,
              bytes_out: int = 0, bytes_in: int = 0,
              ok_responses: int = 0, error_responses: int = 0) -> None:
    """Bump per-peer counters and stamp ``last_activity_at``. Caller passes
    whichever deltas apply; zero-valued kwargs are ignored."""
    if not peer_id:
        return
    b = _peer(peer_id)
    if emits_sent: b.emits_sent += emits_sent
    if emits_received: b.emits_received += emits_received
    if forwards_attempted: b.forwards_attempted += forwards_attempted
    if forwards_incoming: b.forwards_incoming += forwards_incoming
    if dedup_drops: b.dedup_drops += dedup_drops
    if bytes_out: b.bytes_out += bytes_out
    if bytes_in: b.bytes_in += bytes_in
    if ok_responses: b.ok_responses += ok_responses
    if error_responses: b.error_responses += error_responses
    b.last_activity_at = time.time()


def record_event(kind: str, **fields: Any) -> None:
    """Append a bridge event to the rolling log (default 200 entries).
    ``kind`` is a short verb like "forward_sent", "forward_received",
    "delivered_local", "dedup_drop", "peer_down". Callers must keep the
    payload small — this is a live-view convenience, not an audit log.
    """
    ev = {"ts": time.time(), "kind": kind, **fields}
    with _events_lock:
        _counters.events.append(ev)


def recent_events(limit: int = 50) -> list[dict]:
    """Return the newest `limit` bridge events, oldest-first."""
    with _events_lock:
        out = list(_counters.events)
    return out[-limit:]


def per_peer_snapshot() -> list[dict]:
    """Per-peer counter rows, sorted by most-recent activity first.

    We merge in the peer_registry display data (name, host, port,
    server_id) so the admin UI has a single row per bridge — the
    dashboard doesn't have to JOIN on its own."""
    rows: list[dict] = []
    for pid, c in _counters.per_peer.items():
        rows.append({
            "server_id": pid,
            "emits_sent": c.emits_sent,
            "emits_received": c.emits_received,
            "forwards_attempted": c.forwards_attempted,
            "forwards_incoming": c.forwards_incoming,
            "dedup_drops": c.dedup_drops,
            "bytes_out": c.bytes_out,
            "bytes_in": c.bytes_in,
            "ok_responses": c.ok_responses,
            "error_responses": c.error_responses,
            "last_activity_at": c.last_activity_at,
            "idle_seconds": round(time.time() - c.last_activity_at, 1) if c.last_activity_at else None,
        })
    rows.sort(key=lambda r: r.get("last_activity_at") or 0, reverse=True)
    return rows


def snapshot() -> dict[str, Any]:
    """Return a JSON-serialisable view of every counter."""
    from app.core.federation_auth import nonce_cache_size
    from app.services.federation_service import breaker_snapshot
    from app.services.relay_worker import relay_manager

    uptime = time.time() - _counters.started_at
    return {
        "uptime_seconds": round(uptime, 2),
        "hmac": {
            "verified_ok": _counters.hmac_verified_ok,
            "verified_fail": dict(_counters.hmac_verified_fail),
            "nonce_cache_size": nonce_cache_size(),
        },
        "relay": {
            "alloc_ok": _counters.relay_alloc_ok,
            "alloc_quota_denied": _counters.relay_alloc_quota_denied,
            "alloc_rate_limited": _counters.relay_alloc_rate_limited,
            "released": _counters.relay_released,
            "janitor_reaped": _counters.relay_janitor_reaped,
            "active_sessions": relay_manager.session_count(),
            "chains_built": _counters.relay_chains_built,
            "chains_failed": _counters.relay_chains_failed,
        },
        "presence": {
            "pushes_received": _counters.presence_pushes_received,
            "pushes_sent": _counters.presence_pushes_sent,
        },
        "breakers": breaker_snapshot(),
        "bridges": per_peer_snapshot(),
        "recent_events": recent_events(50),
    }


def reset() -> None:
    """Test-only helper."""
    global _counters
    _counters = _Counters()
