"""
ICE configuration service.

Produces the ``ice_servers`` list that gets embedded in the call signaling
payload so the Electron clients can configure ``RTCPeerConnection``
with a ``RTCConfiguration`` that has working STUN + TURN relay fallback.

Why this exists
---------------
Previously the desktop client hard-coded ``iceServers: []`` — every call
negotiated on pure host/srflx candidates and silently failed whenever
mDNS was disabled, Windows Firewall blocked the direct path, or the
participants sat behind different L3 segments (multi-VLAN office LANs).
This module fixes that by handing out short-term TURN credentials on
every call signal that matters:

  - ``call:peer_ready`` (1:1 accept)
  - ``call:peer_joined`` / ``call:group_ringing`` (group join)
  - ``call_participant_joined`` / ``call_incoming`` (v2 group join)
  - ``topology_switch``
  - on-demand refresh via ``call_get_ice_servers``

Design properties
-----------------
  * Per-user ephemeral credentials (HMAC-SHA1 short-term auth).
  * Credentials rotate every :attr:`Settings.TURN_CREDENTIAL_TTL_SECONDS`.
    Clients can re-request on demand before expiry.
  * Auto-detects the bindable LAN IP (``socket.gethostbyname`` + UDP
    ``connect()`` trick) when ``ICE_ANNOUNCED_IP`` is unset.
  * Returns both UDP and TCP TURN URIs so Windows Firewall-dropped UDP
    can still get through via TCP/443 if the deployment uses it.
  * Optional ``turns:`` entry (TLS) when ``TURN_ENABLE_TLS`` is set.
  * Optional ``iceTransportPolicy="relay"`` via ``ICE_FORCE_RELAY`` for
    QA runs that want to verify the relay path end-to-end.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.turn_service import turn_service

logger = get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# LAN IP detection
# ─────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _detected_lan_ip() -> str:
    """
    Best-effort LAN IP discovery.

    The UDP ``connect()`` trick asks the kernel which interface would be
    used to reach an arbitrary public address — no packet is actually
    sent. We then cache the result for the lifetime of the process.

    Falls back to ``127.0.0.1`` on any failure so the downstream call
    never raises.
    """
    ip = "127.0.0.1"
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        # 203.0.113.1 is TEST-NET-3 (RFC 5737) — nothing actually sent.
        sock.connect(("203.0.113.1", 1))
        ip = sock.getsockname()[0]
    except Exception:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return ip


def announced_ip() -> str:
    """Return the IP announced to clients in ICE URIs."""
    return settings.ICE_ANNOUNCED_IP or _detected_lan_ip()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ICEServer:
    """Serializable ICE server entry matching the WebRTC ``RTCIceServer`` dict."""

    urls: list[str]
    username: str | None = None
    credential: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"urls": self.urls}
        if self.username:
            d["username"] = self.username
        if self.credential:
            d["credential"] = self.credential
        return d


def _build_stun_urls() -> list[str]:
    """Build STUN URI list from config or autodetection."""
    if settings.STUN_URIS.strip():
        return [
            u.strip()
            for u in settings.STUN_URIS.split(",")
            if u.strip()
        ]
    return [f"stun:{announced_ip()}:{settings.STUN_PORT}"]


def _build_turn_urls() -> list[str]:
    """Build TURN URI list from config or autodetection."""
    if settings.TURN_URIS.strip():
        return [
            u.strip()
            for u in settings.TURN_URIS.split(",")
            if u.strip()
        ]
    ip = announced_ip()
    urls: list[str] = [
        f"turn:{ip}:{settings.TURN_PORT}?transport=udp",
        f"turn:{ip}:{settings.TURN_PORT}?transport=tcp",
    ]
    if settings.TURN_ENABLE_TLS:
        urls.append(f"turns:{ip}:{settings.TURN_TLS_PORT}?transport=tcp")
    return urls


def build_ice_servers(
    user_id: str,
    *,
    ttl_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """
    Assemble the ICE-server list for ``user_id``.

    Returns a list of plain dicts matching WebRTC ``RTCIceServer``:
        [
            {"urls": ["stun:..."]},
            {"urls": ["turn:..."], "username": "...", "credential": "..."}
        ]

    Always safe to call — on any TURN service failure we still return the
    STUN entry so host/srflx negotiation keeps working.
    """
    entries: list[ICEServer] = []

    try:
        entries.append(ICEServer(urls=_build_stun_urls()))
    except Exception as e:
        logger.warning("ice_stun_build_failed", error=str(e))

    try:
        ttl = int(
            ttl_seconds
            if ttl_seconds is not None
            else settings.TURN_CREDENTIAL_TTL_SECONDS
        )
        creds = turn_service.generate_credentials(user_id, ttl_seconds=ttl)
        entries.append(
            ICEServer(
                urls=_build_turn_urls(),
                username=creds["username"],
                credential=creds["password"],
            )
        )
    except Exception as e:
        logger.warning("ice_turn_build_failed", user_id=user_id, error=str(e))

    return [e.to_dict() for e in entries]


def build_ice_config(
    user_id: str,
    *,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """
    Full ``RTCConfiguration``-shaped dict for clients that want to pass it
    through directly.

        {
            "ice_servers": [...],
            "ice_transport_policy": "all" | "relay",
            "ttl_seconds": 3600,
            "realm": "commclient.local",
        }
    """
    ice_servers = build_ice_servers(user_id, ttl_seconds=ttl_seconds)
    policy = "relay" if settings.ICE_FORCE_RELAY else "all"
    return {
        "ice_servers": ice_servers,
        "ice_transport_policy": policy,
        "ttl_seconds": int(
            ttl_seconds
            if ttl_seconds is not None
            else settings.TURN_CREDENTIAL_TTL_SECONDS
        ),
        "realm": turn_service.realm,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache invalidation helper (for unit tests)
# ─────────────────────────────────────────────────────────────────────────────


def _reset_lan_ip_cache() -> None:
    """Clear the memoized LAN IP. For unit tests only."""
    _detected_lan_ip.cache_clear()
