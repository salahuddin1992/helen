"""UDP hole-punch client — *skeleton*.

Implements the signaling half of RFC 5389 STUN-style hole punching via the
Helen-Rendezvous ``/signal/*`` endpoints. The second half (ICE candidate
negotiation, symmetric-NAT fallback, keep-alive scheduling) is out of
scope for the initial drop — it needs a multi-session coordinator and
pairing protocol that live on top of the signaling primitives here.

What's here today:

  * :func:`register_endpoint` — POST ``/signal/register`` with our observed
    public UDP endpoint so peers can look us up.
  * :func:`lookup_peer` — GET ``/signal/lookup/<id>`` to learn another
    Helen server's advertised endpoint.
  * :func:`discover_external_endpoint` — lightweight STUN-over-HTTP via
    ``/signal/whoami`` (gives observed IP; port is *not* learned without a
    real STUN bind-request — good enough for cone NATs, unsafe for
    symmetric NATs).

Hook the real hole-punch into this module later by:
  1. Opening a UDP socket locally on a specific ephemeral port.
  2. Sending a small probe packet through a third-party public STUN server
     to learn the (mapped_ip, mapped_port) tuple.
  3. Posting that tuple via :func:`register_endpoint`.
  4. Periodically sending keep-alives on the same socket so the NAT
     binding stays alive.
  5. On peer request, pulling their endpoint via :func:`lookup_peer` and
     firing a burst of UDP packets to it — simultaneously, both sides do
     this, NAT holes line up, direct P2P channel is live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


DEFAULT_TIMEOUT = 5.0


@dataclass
class HolePunchConfig:
    rendezvous_http_url: str       # e.g. "http://my-vps.example:9090"
    token: str
    public_id: str
    # Public STUN server used by the future binding-discovery loop.
    # Configurable so air-gapped operators can point at an internal STUN
    # without code changes. Default is Google's well-known anycast pair —
    # safe to leave as-is for typical LAN+VPS deployments.
    stun_server: str = "stun.l.google.com:19302"
    stun_server_alt: str = "stun1.l.google.com:19302"


class HolePunchClient:
    def __init__(self, cfg: HolePunchConfig) -> None:
        if not cfg.rendezvous_http_url or not cfg.token or not cfg.public_id:
            raise ValueError("HolePunchConfig needs rendezvous URL, token, and public_id")
        self.cfg = cfg

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}"}

    async def register_endpoint(self, udp_endpoint: str, meta: dict | None = None) -> dict[str, Any]:
        """Advertise our post-NAT ``ip:port`` so peers can hole-punch to us."""
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{self.cfg.rendezvous_http_url}/signal/register",
                headers=self._auth_header(),
                json={
                    "public_id": self.cfg.public_id,
                    "udp_endpoint": udp_endpoint,
                    "meta": meta or {},
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def lookup_peer(self, peer_public_id: str) -> dict[str, Any] | None:
        """Retrieve another server's advertised endpoint."""
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                resp = await client.get(
                    f"{self.cfg.rendezvous_http_url}/signal/lookup/{peer_public_id}",
                    headers=self._auth_header(),
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as e:
                logger.warning("hole_punch_lookup_error",
                               peer=peer_public_id, error=str(e))
                return None

    async def discover_external_endpoint(self) -> dict[str, Any]:
        """Best-effort STUN-over-HTTP. Returns ``{"observed_ip": "..."}``.

        Note the port is **not** discovered here — HTTP uses a TCP connection
        with its own NAT mapping that won't match what a fresh UDP socket
        would get. For a truly accurate hole-punch bootstrap, pair this with
        a real STUN Binding Request over UDP to a public STUN server.
        """
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                f"{self.cfg.rendezvous_http_url}/signal/whoami",
                headers=self._auth_header(),
            )
            resp.raise_for_status()
            return resp.json()
