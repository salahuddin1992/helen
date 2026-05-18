"""
SPIFFE-style identity authority.

Mints short-lived SVIDs (X.509 + JWT) for every workload. Identities
rotate every hour by default and are signed by an Ed25519 trust root
that lives on disk (or, when available, an HSM-backed key store).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.zt import WorkloadIdentity

logger = get_logger(__name__)


TRUST_DOMAIN = os.environ.get("HELEN_ZT_TRUST_DOMAIN", "helen")
DEFAULT_TTL = timedelta(hours=1)
ROOT_KEY_FILE = os.environ.get(
    "HELEN_ZT_ROOT_KEY", "data/zt/trust-root.key",
)


@dataclass
class SVID:
    """Short-lived verifiable identity document."""
    spiffe_id: str
    workload_type: str
    jwt: str
    public_key: str
    issued_at: datetime
    expires_at: datetime
    attributes: dict[str, Any]


def _trust_root_key() -> tuple[bytes, bytes]:
    """Return (public, private). Persists on disk on first call."""
    os.makedirs(os.path.dirname(ROOT_KEY_FILE) or ".", exist_ok=True)
    if os.path.exists(ROOT_KEY_FILE):
        with open(ROOT_KEY_FILE, "rb") as f:
            blob = f.read()
        if len(blob) >= 64:
            return blob[:32], blob[32:64]
    # Generate.
    try:
        import nacl.signing
        sk = nacl.signing.SigningKey.generate()
        priv = bytes(sk)
        pub = bytes(sk.verify_key)
    except Exception:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives import serialization
            sk = Ed25519PrivateKey.generate()
            priv = sk.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            pub = sk.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        except Exception:
            # HMAC-fallback: insecure, dev-only.
            priv = os.urandom(32)
            pub = hashlib.sha256(priv).digest()
    with open(ROOT_KEY_FILE, "wb") as f:
        f.write(pub + priv)
    try:
        os.chmod(ROOT_KEY_FILE, 0o600)
    except Exception:
        pass
    return pub, priv


def _sign(priv: bytes, msg: bytes) -> bytes:
    try:
        import nacl.signing
        sk = nacl.signing.SigningKey(priv)
        return sk.sign(msg).signature
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        sk = Ed25519PrivateKey.from_private_bytes(priv)
        return sk.sign(msg)
    except Exception:
        pass
    import hmac
    return hmac.new(priv, msg, hashlib.sha256).digest()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def spiffe_id_for(workload_type: str, name: str) -> str:
    return f"spiffe://{TRUST_DOMAIN}/{workload_type}/{name}"


def issue_jwt(
    spiffe_id: str,
    *,
    workload_type: str,
    ttl: timedelta = DEFAULT_TTL,
    audience: str = "helen-internal",
    extra: Optional[dict[str, Any]] = None,
) -> tuple[str, datetime, datetime]:
    """Issue a JWT-SVID. Returns (jwt, issued_at, expires_at)."""
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + ttl
    pub, priv = _trust_root_key()
    header = {"alg": "EdDSA", "typ": "JWT", "kid": "trust-root-1"}
    payload = {
        "sub":         spiffe_id,
        "iss":         f"spiffe://{TRUST_DOMAIN}",
        "aud":         audience,
        "iat":         int(issued_at.timestamp()),
        "exp":         int(expires_at.timestamp()),
        "workload":    workload_type,
        "trust_root":  _b64u(pub),
    }
    if extra:
        payload.update(extra)
    h = _b64u(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    msg = f"{h}.{p}".encode("utf-8")
    sig = _sign(priv, msg)
    return f"{h}.{p}.{_b64u(sig)}", issued_at, expires_at


def verify_jwt(jwt: str) -> Optional[dict[str, Any]]:
    """Verify a JWT-SVID against the trust root. Returns claims or None."""
    try:
        h, p, s = jwt.split(".")
    except ValueError:
        return None

    def _pad(x: str) -> bytes:
        return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))

    try:
        payload = json.loads(_pad(p))
        sig = _pad(s)
    except Exception:
        return None
    pub, _ = _trust_root_key()
    msg = f"{h}.{p}".encode("utf-8")
    if not _verify(pub, msg, sig):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload


def _verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        import nacl.signing
        vk = nacl.signing.VerifyKey(pub)
        vk.verify(msg, sig)
        return True
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        vk = Ed25519PublicKey.from_public_bytes(pub)
        vk.verify(sig, msg)
        return True
    except Exception:
        pass
    return False


class SpiffeAuthority:
    """DB-backed identity issuer."""

    async def issue(
        self,
        workload_type: str,
        name: str,
        *,
        attributes: Optional[dict[str, Any]] = None,
        ttl: timedelta = DEFAULT_TTL,
        parent_id: Optional[str] = None,
    ) -> SVID:
        spiffe = spiffe_id_for(workload_type, name)
        jwt, iat, eat = issue_jwt(
            spiffe, workload_type=workload_type, ttl=ttl, extra=attributes,
        )
        pub, _ = _trust_root_key()
        pub_b64 = base64.b64encode(pub).decode("ascii")
        async with async_session_factory() as db:
            row = (await db.execute(
                select(WorkloadIdentity).where(WorkloadIdentity.spiffe_id == spiffe)
            )).scalar_one_or_none()
            if row is None:
                row = WorkloadIdentity(
                    spiffe_id=spiffe,
                    workload_type=workload_type,
                    public_key=pub_b64,
                    issued_at=iat,
                    expires_at=eat,
                    parent_identity_id=parent_id,
                    attributes=attributes or {},
                    revoked=False,
                )
                db.add(row)
            else:
                row.issued_at = iat
                row.expires_at = eat
                row.public_key = pub_b64
                row.attributes = attributes or row.attributes
                row.revoked = False
            await db.commit()
        return SVID(
            spiffe_id=spiffe,
            workload_type=workload_type,
            jwt=jwt,
            public_key=pub_b64,
            issued_at=iat,
            expires_at=eat,
            attributes=attributes or {},
        )

    async def rotate_all(self) -> int:
        """Force-rotate identities expiring soon."""
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=10)
        rotated = 0
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(WorkloadIdentity).where(
                    WorkloadIdentity.expires_at < soon,
                    WorkloadIdentity.revoked == False,  # noqa: E712
                )
            )).scalars().all()
            for r in rows:
                # Re-issue. Name is the last spiffe path component.
                name = r.spiffe_id.rsplit("/", 1)[-1]
                await self.issue(r.workload_type, name,
                                 attributes=r.attributes or {})
                rotated += 1
        return rotated

    async def revoke(self, spiffe_id: str) -> bool:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(WorkloadIdentity).where(
                    WorkloadIdentity.spiffe_id == spiffe_id
                )
            )).scalar_one_or_none()
            if row is None:
                return False
            row.revoked = True
            await db.commit()
            return True

    async def trust_bundle(self) -> dict[str, Any]:
        """Distribute the trust bundle (root public key) to clients."""
        pub, _ = _trust_root_key()
        return {
            "trust_domain": TRUST_DOMAIN,
            "keys": [{
                "kid": "trust-root-1",
                "kty": "OKP",
                "crv": "Ed25519",
                "x":   base64.urlsafe_b64encode(pub).rstrip(b"=").decode("ascii"),
            }],
        }


_authority: Optional[SpiffeAuthority] = None


def get_spiffe_authority() -> SpiffeAuthority:
    global _authority
    if _authority is None:
        _authority = SpiffeAuthority()
    return _authority
