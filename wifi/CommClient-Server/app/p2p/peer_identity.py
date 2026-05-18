"""Peer identity utilities — local server_id + pubkey shape."""

from __future__ import annotations

import hashlib


def my_peer_id() -> str:
    try:
        from app.services.discovery_service import get_server_id
        return get_server_id() or "anon"
    except Exception:
        return "anon"


def my_cluster_id() -> str:
    try:
        from app.core.config import get_settings
        return get_settings().COMMCLIENT_CLUSTER_ID or "default"
    except Exception:
        return "default"


def fingerprint(peer_id: str, pubkey: str = "") -> str:
    """Stable short fingerprint for logs / UI — sha256(peer_id + pubkey)
    truncated to 16 hex chars."""
    h = hashlib.sha256()
    h.update(peer_id.encode())
    if pubkey:
        h.update(b":")
        h.update(pubkey.encode())
    return h.hexdigest()[:16]


def is_self(peer_id: str) -> bool:
    return peer_id == my_peer_id()


def identity_snapshot() -> dict:
    return {
        "peer_id":     my_peer_id(),
        "cluster_id":  my_cluster_id(),
        "fingerprint": fingerprint(my_peer_id()),
    }
