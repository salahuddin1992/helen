"""
Federation v2 — addressing & resolution.

Address grammar
---------------
* User address:    ``localpart@server.example``      (RFC-5321-like)
* Channel address: ``#channelid@server.example``     (Matrix-inspired)

Server resolution
-----------------
``resolve_server(domain)`` discovers the canonical Helen endpoint for a
domain using the following waterfall:

    1. Local DB cache (``federation_v2_servers``).
    2. DNS SRV: ``_helen._tcp.<domain>``
    3. ``.well-known/helen-federation``  (HTTPS GET)
    4. Bare HTTPS on the domain  (last-resort).

The DNS path is optional — if ``dnspython`` is unavailable we skip
straight to .well-known.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.federation_v2 import FederatedServer

logger = get_logger(__name__)


# RFC-1123-ish DNS label; total length ≤ 253.
_SERVER_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$"
)
_LOCALPART_RE = re.compile(r"^[a-z0-9._\-]{1,64}$", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"^#?[a-zA-Z0-9._\-]{1,64}$")


@dataclass(frozen=True)
class Address:
    """Parsed federation address."""

    localpart: str
    server: str
    kind: str  # "user" | "channel"

    @property
    def canonical(self) -> str:
        return f"{self.localpart}@{self.server}"


class AddressError(ValueError):
    """Invalid address grammar."""


def _validate_server_id(domain: str) -> str:
    domain = (domain or "").strip().lower()
    if not domain:
        raise AddressError("empty server id")
    if not _SERVER_RE.match(domain):
        raise AddressError(f"invalid server id: {domain!r}")
    return domain


def parse_address(s: str) -> Address:
    """Parse a ``user@server`` address. Raises AddressError on grammar
    failures."""
    if not s or "@" not in s:
        raise AddressError(f"missing @ separator in {s!r}")
    local, _, server = s.rpartition("@")
    if not _LOCALPART_RE.match(local or ""):
        raise AddressError(f"invalid localpart: {local!r}")
    server = _validate_server_id(server)
    return Address(localpart=local, server=server, kind="user")


def parse_channel(s: str) -> Address:
    """Parse a ``#channel@server`` address. The ``#`` prefix is optional."""
    if not s or "@" not in s:
        raise AddressError(f"missing @ separator in {s!r}")
    local, _, server = s.rpartition("@")
    if not _CHANNEL_RE.match(local or ""):
        raise AddressError(f"invalid channel localpart: {local!r}")
    local = local.lstrip("#")
    server = _validate_server_id(server)
    return Address(localpart=local, server=server, kind="channel")


async def _srv_lookup(domain: str) -> Optional[tuple[str, int]]:
    """``_helen._tcp.<domain>`` SRV lookup. Returns (host, port) or None."""
    try:
        import dns.resolver  # type: ignore[import-untyped]
    except Exception:
        return None
    try:
        ans = dns.resolver.resolve(f"_helen._tcp.{domain}", "SRV")
    except Exception:
        return None
    best = None
    for rr in ans:
        prio = int(getattr(rr, "priority", 0))
        if best is None or prio < best[0]:
            best = (prio, str(rr.target).rstrip("."), int(rr.port))
    if best is None:
        return None
    return best[1], best[2]


async def _wellknown_lookup(domain: str) -> Optional[dict]:
    """Fetch ``https://<domain>/.well-known/helen-federation``."""
    try:
        import httpx  # type: ignore[import-untyped]
    except Exception:
        return None
    url = f"https://{domain}/.well-known/helen-federation"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as cli:
            r = await cli.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
            if not isinstance(data, dict):
                return None
            return data
    except Exception as exc:
        logger.debug("wellknown_lookup_failed domain=%s err=%s", domain, exc)
        return None


async def resolve_server(
    domain: str,
    db: Optional[AsyncSession] = None,
    *,
    refresh: bool = False,
) -> Optional[FederatedServer]:
    """Resolve a remote Helen server. Returns the cached row if any,
    else a freshly-built row (NOT persisted — caller decides)."""
    server_id = _validate_server_id(domain)

    async def _load(db: AsyncSession) -> Optional[FederatedServer]:
        r = await db.execute(
            select(FederatedServer).where(FederatedServer.server_id == server_id)
        )
        return r.scalar_one_or_none()

    if db is None:
        async with async_session_factory() as _db:
            row = await _load(_db)
    else:
        row = await _load(db)

    if row is not None and not refresh:
        return row

    # Discovery — DNS SRV first.
    srv = await _srv_lookup(server_id)
    advertise_url: Optional[str] = None
    if srv:
        host, port = srv
        scheme = "https" if port in (443, 8443) else "https"
        advertise_url = f"{scheme}://{host}:{port}"

    info = await _wellknown_lookup(server_id) or {}
    if not advertise_url:
        advertise_url = (
            info.get("advertise_url")
            or info.get("base_url")
            or f"https://{server_id}"
        )
    pubkey = info.get("public_key") or ""
    version = info.get("version") or ""
    capabilities = info.get("capabilities") or {}
    signing_algo = info.get("signing_algo") or "ed25519"

    if row is None:
        return FederatedServer(
            server_id=server_id,
            advertise_url=advertise_url,
            public_key=pubkey,
            version=version,
            capabilities=capabilities,
            signing_algo=signing_algo,
            status="pending",
            trust_level="peer",
        )
    row.advertise_url = advertise_url or row.advertise_url
    if pubkey:
        row.public_key = pubkey
    if version:
        row.version = version
    if capabilities:
        row.capabilities = capabilities
    if signing_algo:
        row.signing_algo = signing_algo
    return row


def my_server_id() -> str:
    """Return this server's federation identity. Derived from settings."""
    try:
        from app.core.config import get_settings
        s = get_settings()
        sid = getattr(s, "FEDERATION_V2_SERVER_ID", None) or getattr(
            s, "HELEN_SERVER_ID", None
        )
        if sid:
            return _validate_server_id(str(sid))
    except Exception:
        pass
    import socket
    return _validate_server_id(socket.getfqdn() or "helen.local")
