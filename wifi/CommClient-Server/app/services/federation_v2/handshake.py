"""
Federation v2 — server-to-server handshake.

Flow
----
1.  ``GET /.well-known/helen-federation`` is the discovery card; the
    initiating server fetches the remote card.
2.  The initiator POSTs its own card to ``/api/_federation/v2/handshake``.
    The body contains a ``challenge`` nonce.
3.  The responding server verifies the initiator's signature, replies
    with its own signed acknowledgement + counter-challenge.
4.  Initiator verifies, persists row in ``federation_v2_servers``.

Capability negotiation is implicit — the responder echoes the subset of
capabilities it understands.
"""
from __future__ import annotations

import base64
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.federation_v2 import FederatedServer
from app.services.federation_v2.addressing import (
    Address, AddressError, _validate_server_id, my_server_id, resolve_server,
)
from app.services.federation_v2.signing import (
    canonical_json, get_local_signing_key, sign, verify,
)

logger = get_logger(__name__)


PROTOCOL_VERSION = "fedv2-1.0"
SUPPORTED_CAPABILITIES = {
    "events.dag":        True,
    "channels.share":    True,
    "presence":          True,
    "typing":            True,
    "reactions":         True,
    "trust.web":         True,
    "sync.incremental":  True,
    "backfill":          True,
}


@dataclass
class ServerCard:
    """The .well-known card. JSON-serialisable."""
    server_id: str
    public_key: str
    advertise_url: str
    version: str
    signing_algo: str
    capabilities: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id":     self.server_id,
            "public_key":    self.public_key,
            "advertise_url": self.advertise_url,
            "version":       self.version,
            "signing_algo":  self.signing_algo,
            "capabilities":  self.capabilities,
            "protocol":      PROTOCOL_VERSION,
        }


def my_server_card() -> ServerCard:
    """Build this server's .well-known card."""
    sk = get_local_signing_key()
    sid = my_server_id()
    advertise = (
        os.environ.get("HELEN_FEDV2_ADVERTISE_URL")
        or f"https://{sid}"
    )
    return ServerCard(
        server_id=sid,
        public_key=sk.pub_b64(),
        advertise_url=advertise,
        version=os.environ.get("HELEN_VERSION", "7.0.0"),
        signing_algo="ed25519",
        capabilities=dict(SUPPORTED_CAPABILITIES),
    )


def make_challenge(server_id: str) -> dict[str, Any]:
    """Construct a fresh signed challenge."""
    sk = get_local_signing_key()
    nonce = base64.b64encode(secrets.token_bytes(24)).decode("ascii")
    body = {
        "type":     "challenge",
        "from":     my_server_id(),
        "to":       server_id,
        "nonce":    nonce,
        "issued":   int(time.time()),
    }
    sig = sign(sk, canonical_json(body))
    body["signature"] = base64.b64encode(sig).decode("ascii")
    return body


def verify_challenge(challenge: dict[str, Any], public_key_b64: str) -> bool:
    """Verify a peer-issued challenge."""
    sig_b64 = challenge.get("signature") or ""
    body = {k: v for k, v in challenge.items() if k != "signature"}
    if not sig_b64:
        return False
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False
    # Reject very old challenges (>5 min).
    issued = int(body.get("issued") or 0)
    if abs(time.time() - issued) > 300:
        return False
    return verify(public_key_b64, canonical_json(body), sig)


async def begin_handshake(
    remote_domain: str,
    *,
    timeout: float = 10.0,
) -> Optional[FederatedServer]:
    """Drive a handshake against ``remote_domain``. Persists the result."""
    try:
        sid = _validate_server_id(remote_domain)
    except AddressError as exc:
        logger.warning("fedv2_handshake_bad_domain domain=%s err=%s", remote_domain, exc)
        return None

    try:
        import httpx
    except Exception:
        logger.warning("fedv2_handshake_no_httpx")
        return None

    async with async_session_factory() as db:
        peer = await resolve_server(sid, db=db, refresh=True)
        if peer is None:
            return None

        my_card = my_server_card()
        challenge = make_challenge(sid)
        body = {
            "card":       my_card.to_dict(),
            "challenge":  challenge,
        }
        url = peer.advertise_url.rstrip("/") + "/api/_federation/v2/handshake"
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=True) as cli:
                r = await cli.post(url, json=body)
        except Exception as exc:
            logger.warning("fedv2_handshake_connect_failed domain=%s err=%s", sid, exc)
            return None

        if r.status_code != 200:
            logger.warning("fedv2_handshake_bad_status domain=%s code=%s",
                           sid, r.status_code)
            return None
        try:
            response = r.json()
        except Exception:
            return None

        peer_card = response.get("card") or {}
        peer_pubkey = peer_card.get("public_key") or ""
        if not peer_pubkey:
            logger.warning("fedv2_handshake_no_pubkey domain=%s", sid)
            return None
        # Verify the counter-challenge that proves the peer holds the key.
        ack = response.get("ack_signature") or ""
        ack_payload = {
            "type":      "ack",
            "from":      sid,
            "to":        my_card.server_id,
            "nonce":     challenge.get("nonce"),
            "issued_at": response.get("issued_at"),
        }
        try:
            ack_sig = base64.b64decode(ack)
        except Exception:
            return None
        if not verify(peer_pubkey, canonical_json(ack_payload), ack_sig):
            logger.warning("fedv2_handshake_bad_ack domain=%s", sid)
            return None

        # Persist or update peer state.
        existing = (await db.execute(
            select(FederatedServer).where(FederatedServer.server_id == sid)
        )).scalar_one_or_none()
        if existing is None:
            existing = FederatedServer(
                server_id=sid,
                public_key=peer_pubkey,
                advertise_url=peer_card.get("advertise_url") or peer.advertise_url,
                version=peer_card.get("version") or "",
                capabilities=peer_card.get("capabilities") or {},
                signing_algo=peer_card.get("signing_algo") or "ed25519",
                status="active",
                trust_level="peer",
                trust_score=0.5,
            )
            db.add(existing)
        else:
            existing.public_key = peer_pubkey
            existing.advertise_url = peer_card.get("advertise_url") or existing.advertise_url
            existing.version = peer_card.get("version") or existing.version
            existing.capabilities = peer_card.get("capabilities") or existing.capabilities
            existing.signing_algo = peer_card.get("signing_algo") or existing.signing_algo
            existing.status = "active"
        await db.commit()
        await db.refresh(existing)
        return existing


def negotiate_capabilities(peer_caps: dict[str, Any]) -> dict[str, Any]:
    """Return the intersection of supported capabilities."""
    out: dict[str, Any] = {}
    for k, v in SUPPORTED_CAPABILITIES.items():
        if peer_caps.get(k):
            out[k] = bool(v)
    return out
