"""Peer handshake — verify identity + cluster_id before promoting.

The lower layer's HMAC verify is enough for steady-state federation
calls; this module is the *handshake-time* protocol that newly
discovered peers run before being moved into ACTIVE state.

Logical flow:

    1. Receiver gets peer-announce body (id, host, cluster_id, ts).
    2. Sender signs the body with the cluster HMAC key (auto-derived).
    3. Receiver verifies, then promotes the peer's lifecycle from
       DISCOVERED → AUTHENTICATING → ACTIVE.

Failed verifications stay DISCOVERED and feed the trust score
``bad_signature`` event.
"""

from __future__ import annotations

import time

from app.p2p.peer_events import emit
from app.p2p.peer_lifecycle import PeerState, get_lifecycle
from app.p2p.p2p_exceptions import PeerHandshakeError


def announce_payload(peer_id: str, cluster_id: str,
                     host: str, port: int) -> dict:
    """Build the announce body. Caller wraps it in a signed header
    before sending."""
    return {
        "peer_id":    peer_id,
        "cluster_id": cluster_id,
        "host":       host,
        "port":       port,
        "ts":         int(time.time()),
    }


def verify_announce(payload: dict, expected_cluster: str) -> tuple[bool, str]:
    """Verify a received announce dict. Returns (ok, reason)."""
    if not isinstance(payload, dict):
        return False, "bad_payload"
    pid = str(payload.get("peer_id") or "")
    if not pid:
        return False, "missing_peer_id"
    cluster = str(payload.get("cluster_id") or "")
    if cluster != expected_cluster:
        return False, "cluster_mismatch"
    ts = int(payload.get("ts") or 0)
    if abs(time.time() - ts) > 120:
        return False, "stale_timestamp"
    return True, "ok"


def perform_inbound_handshake(payload: dict) -> bool:
    """Apply the handshake on an incoming announce — moves the peer
    through the lifecycle state machine and records trust events."""
    from app.p2p.peer_identity import my_cluster_id
    ok, reason = verify_announce(payload, my_cluster_id())
    pid = str(payload.get("peer_id") or "")
    lc = get_lifecycle()
    if not ok:
        emit("handshake.failed", {"peer_id": pid, "reason": reason})
        try:
            from app.services.trust_score import get_trust_db
            ev = ("cluster_mismatch" if reason == "cluster_mismatch"
                  else "bad_signature")
            get_trust_db().record_event(pid, ev)
        except Exception:
            pass
        return False

    lc.transition(pid, PeerState.AUTHENTICATING)
    lc.transition(pid, PeerState.ACTIVE)
    emit("handshake.ok", {"peer_id": pid})
    try:
        from app.services.trust_score import get_trust_db
        get_trust_db().record_event(pid, "successful_exchange")
    except Exception:
        pass
    return True


def require_active(peer_id: str) -> None:
    """Raise if the peer hasn't completed a successful handshake."""
    if get_lifecycle().state(peer_id) is not PeerState.ACTIVE:
        raise PeerHandshakeError(f"peer {peer_id!r} not ACTIVE")
