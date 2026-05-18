"""TURN credentials allocator — short-lived users for coturn.

WebRTC media falls back to TURN when direct + STUN both fail. coturn
supports the *long-term credential mechanism* where the TURN server
shares a secret with the application; the app issues users in the
form ``<expiry-unix>:<user_id>`` and the password is the HMAC-SHA1
of that user with the shared secret.

This module:

  1. Reads ``HELEN_TURN_SHARED_SECRET`` (or derives from cluster_id).
  2. Issues credentials valid for ``HELEN_TURN_TTL_SEC`` (default 1h).
  3. Returns the ICE-server config the client passes to RTCPeerConnection.

The TURN server itself (coturn / pion-turn) is operated separately;
this module just *issues* credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except (TypeError, ValueError):
        return default


TURN_TTL_SEC      = _f("HELEN_TURN_TTL_SEC", 3600.0)


def _shared_secret() -> bytes:
    raw = os.environ.get("HELEN_TURN_SHARED_SECRET")
    if raw:
        return raw.encode()
    try:
        from app.core.config import get_settings
        cluster_id = get_settings().COMMCLIENT_CLUSTER_ID or "default"
    except Exception:
        cluster_id = "default"
    return hashlib.sha256(
        f"helen-turn-secret:{cluster_id}".encode()
    ).digest()


def _turn_servers_from_env() -> list[str]:
    raw = os.environ.get("HELEN_TURN_SERVERS", "") or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class TURNCredentials:
    username:    str
    password:    str
    ttl:         int
    expires_at:  int
    urls:        list[str]

    def to_dict(self) -> dict:
        return {
            "username":   self.username,
            "credential": self.password,        # WebRTC RTCIceServer name
            "ttl":        self.ttl,
            "expires_at": self.expires_at,
            "urls":       list(self.urls),
        }


def allocate(user_id: str,
             *, ttl_sec: float | None = None) -> TURNCredentials:
    """Issue a short-lived TURN credential pair for ``user_id``."""
    ttl = int(ttl_sec if ttl_sec is not None else TURN_TTL_SEC)
    expires = int(time.time() + ttl)
    username = f"{expires}:{user_id or 'anon'}"
    secret = _shared_secret()
    digest = hmac.new(secret, username.encode(), hashlib.sha1).digest()
    password = base64.b64encode(digest).decode("ascii")
    return TURNCredentials(
        username=username,
        password=password,
        ttl=ttl,
        expires_at=expires,
        urls=_turn_servers_from_env(),
    )


def ice_servers_for(user_id: str,
                    *, ttl_sec: float | None = None) -> list[dict]:
    """Return a WebRTC RTCIceServer config list including STUN +
    issued TURN credentials."""
    out: list[dict] = []
    stun = os.environ.get("HELEN_STUN_URL", "")
    if stun:
        out.append({"urls": stun})
    turn_urls = _turn_servers_from_env()
    if turn_urls:
        creds = allocate(user_id, ttl_sec=ttl_sec)
        out.append({
            "urls":        turn_urls,
            "username":    creds.username,
            "credential":  creds.password,
        })
    return out


def status() -> dict:
    return {
        "turn_servers":      _turn_servers_from_env(),
        "ttl_sec":           TURN_TTL_SEC,
        "secret_source":     ("HELEN_TURN_SHARED_SECRET env"
                              if os.environ.get("HELEN_TURN_SHARED_SECRET")
                              else "cluster_id derivation"),
        "secret_fingerprint": hashlib.sha256(_shared_secret()).hexdigest()[:16],
    }
