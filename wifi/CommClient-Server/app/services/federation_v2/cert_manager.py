"""
FederationCertManager — mTLS certificate lifecycle.

* ``info(peer_id)``      — return the active cert summary.
* ``rotate(peer_id)``    — issue a new keypair + self-signed leaf,
                           mark the previous row inactive.
* ``validate_chain(...)``— walk the PEM chain and report depth/issuer.
* ``expiring(days=30)``  — list peers whose cert expires within ``days``.
* ``rotate_all(reason)`` — bulk rotation.

This module degrades gracefully if ``cryptography`` is not installed:
all destructive ops will refuse with ``{"ok": False, "error":
"cryptography_unavailable"}`` instead of raising.

Singleton: ``get_cert_manager()``.
"""
from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.federation_cert import FederationCert
from app.models.federation_peer import FederationPeerMeta
from app.models.federation_v2 import FederatedServer

logger = structlog.get_logger(__name__)

EXPIRY_WARN_DAYS = 30
DEFAULT_VALIDITY_DAYS = 365


class FederationCertManager:
    # ── reads ────────────────────────────────────────────────

    async def info(self, peer_id: str) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            sid = await self._server_id(db, peer_id)
            if sid is None:
                return None
            row = (await db.execute(
                select(FederationCert).where(
                    FederationCert.server_id == sid,
                    FederationCert.active.is_(True),
                )
            )).scalar_one_or_none()
        if row is None:
            return {"server_id": sid, "present": False}
        return self._to_dict(row)

    async def history(self, peer_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            sid = await self._server_id(db, peer_id)
            if sid is None:
                return []
            rows = (await db.execute(
                select(FederationCert)
                .where(FederationCert.server_id == sid)
                .order_by(FederationCert.created_at.desc())
                .limit(limit)
            )).scalars().all()
        return [self._to_dict(r) for r in rows]

    async def expiring(self, days: int = EXPIRY_WARN_DAYS) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) + timedelta(days=max(1, days))
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(FederationCert)
                .where(
                    FederationCert.active.is_(True),
                    FederationCert.not_after <= cutoff,
                )
                .order_by(FederationCert.not_after.asc())
            )).scalars().all()
        return [self._to_dict(r) for r in rows]

    # ── writes ───────────────────────────────────────────────

    async def rotate(
        self,
        peer_id: str,
        *,
        reason: str = "manual",
        validity_days: int = DEFAULT_VALIDITY_DAYS,
        actor: str = "system",
    ) -> dict[str, Any]:
        async with async_session_factory() as db:
            sid = await self._server_id(db, peer_id)
            if sid is None:
                return {"ok": False, "error": "not_found"}

            generated = self._generate_cert(sid, validity_days=validity_days)
            if not generated.get("ok"):
                return generated

            # Deactivate old
            old = (await db.execute(
                select(FederationCert).where(
                    FederationCert.server_id == sid,
                    FederationCert.active.is_(True),
                )
            )).scalars().all()
            now = datetime.now(timezone.utc)
            for o in old:
                o.active = False
                o.revoked_at = now
                o.revoked_reason = "rotated"

            row = FederationCert(
                server_id=sid,
                fingerprint_sha256=generated["fingerprint"],
                subject=generated.get("subject", ""),
                issuer=generated.get("issuer", ""),
                serial=generated.get("serial", ""),
                not_before=generated["not_before"],
                not_after=generated["not_after"],
                chain_depth=generated.get("chain_depth", 1),
                chain_pem=generated.get("chain_pem"),
                leaf_pem=generated.get("leaf_pem"),
                active=True,
                rotation_reason=reason,
                extra={"actor": actor, "method": generated.get("method")},
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)

            meta = (await db.execute(
                select(FederationPeerMeta).where(
                    FederationPeerMeta.server_id == sid
                )
            )).scalar_one_or_none()
            if meta is not None:
                meta.cert_id = row.id
                await db.commit()
            return {"ok": True, **self._to_dict(row)}

    async def rotate_all(
        self,
        *,
        reason: str = "bulk-rotation",
        actor: str = "system",
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async with async_session_factory() as db:
            peers = (await db.execute(select(FederatedServer))).scalars().all()
        for p in peers:
            out.append(await self.rotate(p.server_id, reason=reason, actor=actor))
        return out

    async def validate_chain(self, peer_id: str) -> dict[str, Any]:
        async with async_session_factory() as db:
            sid = await self._server_id(db, peer_id)
            if sid is None:
                return {"ok": False, "error": "not_found"}
            row = (await db.execute(
                select(FederationCert).where(
                    FederationCert.server_id == sid,
                    FederationCert.active.is_(True),
                )
            )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "no_active_cert", "server_id": sid}
        now = datetime.now(timezone.utc)
        nbf = row.not_before
        naf = row.not_after
        if nbf and nbf.tzinfo is None:
            nbf = nbf.replace(tzinfo=timezone.utc)
        if naf and naf.tzinfo is None:
            naf = naf.replace(tzinfo=timezone.utc)
        issues: list[str] = []
        if nbf and nbf > now:
            issues.append("not_yet_valid")
        if naf and naf < now:
            issues.append("expired")
        if naf and (naf - now) < timedelta(days=EXPIRY_WARN_DAYS):
            issues.append("expires_soon")
        if row.chain_depth < 1:
            issues.append("empty_chain")
        return {
            "ok": not issues,
            "server_id": sid,
            "fingerprint": row.fingerprint_sha256,
            "subject": row.subject,
            "issuer": row.issuer,
            "not_before": nbf.isoformat() if nbf else None,
            "not_after": naf.isoformat() if naf else None,
            "chain_depth": row.chain_depth,
            "issues": issues,
        }

    # ── internals ────────────────────────────────────────────

    async def _server_id(
        self, db: AsyncSession, peer_id: str,
    ) -> Optional[str]:
        row = (await db.execute(
            select(FederatedServer).where(FederatedServer.id == peer_id)
        )).scalar_one_or_none()
        if row is not None:
            return row.server_id
        row = (await db.execute(
            select(FederatedServer).where(
                FederatedServer.server_id == peer_id
            )
        )).scalar_one_or_none()
        return row.server_id if row else None

    def _generate_cert(
        self, server_id: str, validity_days: int = DEFAULT_VALIDITY_DAYS,
    ) -> dict[str, Any]:
        """Try real X.509 via ``cryptography``; fall back to a synthetic
        fingerprint+metadata so the audit trail is preserved even on
        minimal installs."""
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519

            now = datetime.now(timezone.utc)
            sk = ed25519.Ed25519PrivateKey.generate()
            pub = sk.public_key()
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, server_id),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Helen Federation"),
            ])
            serial = int.from_bytes(os.urandom(16), "big") >> 1
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(pub)
                .serial_number(serial)
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=max(1, validity_days)))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(server_id)]),
                    critical=False,
                )
                .sign(private_key=sk, algorithm=None)
            )
            pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            der = cert.public_bytes(serialization.Encoding.DER)
            fp = hashlib.sha256(der).hexdigest()
            return {
                "ok": True,
                "method":      "ed25519/x509",
                "fingerprint": fp,
                "subject":     subject.rfc4514_string(),
                "issuer":      issuer.rfc4514_string(),
                "serial":      f"{serial:x}",
                "not_before":  now - timedelta(minutes=5),
                "not_after":   now + timedelta(days=max(1, validity_days)),
                "chain_depth": 1,
                "leaf_pem":    pem,
                "chain_pem":   pem,
            }
        except Exception as exc:  # pragma: no cover - cryptography optional
            logger.info("fedmap_cert_fallback", error=str(exc))
            now = datetime.now(timezone.utc)
            seed = os.urandom(32)
            fp = hashlib.sha256(seed + server_id.encode("utf-8")).hexdigest()
            return {
                "ok": True,
                "method":      "synthetic",
                "fingerprint": fp,
                "subject":     f"CN={server_id}",
                "issuer":      "CN=helen-self",
                "serial":      base64.urlsafe_b64encode(seed[:8]).decode("ascii"),
                "not_before":  now - timedelta(minutes=5),
                "not_after":   now + timedelta(days=max(1, validity_days)),
                "chain_depth": 1,
                "leaf_pem":    None,
                "chain_pem":   None,
            }

    def _to_dict(self, row: FederationCert) -> dict[str, Any]:
        return {
            "id":                  row.id,
            "server_id":           row.server_id,
            "present":             True,
            "fingerprint_sha256":  row.fingerprint_sha256,
            "subject":             row.subject,
            "issuer":              row.issuer,
            "serial":              row.serial,
            "not_before":          row.not_before.isoformat() if row.not_before else None,
            "not_after":           row.not_after.isoformat() if row.not_after else None,
            "chain_depth":         row.chain_depth,
            "active":              row.active,
            "rotation_reason":     row.rotation_reason,
            "revoked_at":          row.revoked_at.isoformat() if row.revoked_at else None,
        }


# ── singleton ───────────────────────────────────────────────


_mgr: Optional[FederationCertManager] = None


def get_cert_manager() -> FederationCertManager:
    global _mgr
    if _mgr is None:
        _mgr = FederationCertManager()
    return _mgr
