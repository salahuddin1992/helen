"""Peer connection — outbound HTTP to a peer with adaptive timeout."""

from __future__ import annotations

from typing import Any, Optional

from app.p2p.p2p_config import get_config
from app.p2p.p2p_exceptions import PeerConnectionError
from app.p2p.peer_model import Peer


async def request(
    peer: Peer,
    method: str = "GET",
    path: str = "/",
    body: Any = None,
    headers: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> tuple[int, Any, dict]:
    """One HTTP call to a peer. Uses adaptive_timeout when available.
    Raises PeerConnectionError on transport failure."""
    cfg = get_config()
    if timeout is None:
        try:
            from app.services.adaptive_timeout import timeout_for_peer
            timeout = timeout_for_peer(peer.host, peer.port)
        except Exception:
            timeout = cfg.connect_timeout_sec

    try:
        import httpx
    except ImportError as e:
        raise PeerConnectionError(f"httpx missing: {e}")

    url = f"http://{peer.host}:{peer.port}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, url, json=body,
                                headers=headers or {})
        try:
            return r.status_code, r.json(), dict(r.headers)
        except Exception:
            return r.status_code, r.text, dict(r.headers)
    except Exception as e:
        raise PeerConnectionError(f"{peer.peer_id[:24]}: {e}")
