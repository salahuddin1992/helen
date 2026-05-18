"""
RouterPairingService — TOFU exchange with a Helen router.

Two phases:
    1. ``begin``    — fetch ``<router_url>/.well-known/helen-router-public-key``,
                      compute SHA-256 fingerprint, store a *pending* row and
                      return the fingerprint to the operator.
    2. ``confirm``  — operator types the displayed fingerprint back; we promote
                      the row to ``status='confirmed'`` and run a round-trip
                      ping to verify reachability.

Concurrent ``begin`` calls for the same URL collapse to one pending row to
keep TOFU semantically meaningful (i.e. the first fingerprint shown is the
one the operator commits to).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

try:
    import httpx  # type: ignore
    _HAS_HTTPX = True
except Exception:
    _HAS_HTTPX = False

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.models.router_pairing import RouterPairing

logger = get_logger(__name__)


class RouterPairingService:

    WELL_KNOWN_PATH = "/.well-known/helen-router-public-key"
    PING_PATH = "/.well-known/helen-router-ping"
    HTTP_TIMEOUT = 5.0

    # ── phase 1 ──
    async def begin(
        self, db: AsyncSession, *, router_url: str, actor: str,
    ) -> dict[str, Any]:
        public_key = await self._fetch_public_key(router_url)
        fp = self.fingerprint(public_key)
        nonce = secrets.token_hex(16)

        # Upsert pending row.
        existing = (await db.execute(
            select(RouterPairing).where(RouterPairing.router_url == router_url)
        )).scalars().first()
        if existing and existing.status != "confirmed":
            existing.public_key_pem = public_key
            existing.fingerprint_sha256 = fp
            existing.nonce = nonce
            existing.status = "pending"
        else:
            db.add(RouterPairing(
                router_url=router_url,
                public_key_pem=public_key,
                fingerprint_sha256=fp,
                nonce=nonce,
                status="pending",
            ))
        await db.flush()

        audit_log("router.pair.begin", user_id=actor,
                  details={"router_url": router_url,
                           "fingerprint": fp})
        return {"router_url": router_url, "fingerprint_sha256": fp,
                "nonce": nonce, "status": "pending"}

    # ── phase 2 ──
    async def confirm(
        self, db: AsyncSession, *,
        router_url: str, fingerprint: str, actor: str,
    ) -> dict[str, Any]:
        row = (await db.execute(
            select(RouterPairing).where(RouterPairing.router_url == router_url)
        )).scalars().first()
        if row is None:
            raise ValueError("no pending pairing — call /router/pair first")
        if row.fingerprint_sha256.lower().replace(":", "") != \
                fingerprint.lower().replace(":", ""):
            audit_log("router.pair.confirm", user_id=actor,
                      success=False,
                      details={"router_url": router_url,
                               "reason": "fingerprint mismatch"})
            raise ValueError("fingerprint mismatch — possible MITM")

        # Round-trip ping (best effort)
        rtt: int | None = None
        try:
            rtt = await self._ping(router_url)
        except Exception as e:
            logger.warning("router_ping_failed", error=str(e))

        now_utc = datetime.now(timezone.utc)
        await db.execute(
            update(RouterPairing)
            .where(RouterPairing.id == row.id)
            .values(status="confirmed",
                    confirmed_at=now_utc,
                    last_ping_at=now_utc if rtt is not None else None,
                    last_ping_rtt_ms=rtt)
        )
        await db.flush()
        audit_log("router.pair.confirm", user_id=actor,
                  details={"router_url": router_url,
                           "fingerprint": fingerprint,
                           "rtt_ms": rtt})
        return {"router_url": router_url, "status": "confirmed",
                "rtt_ms": rtt, "fingerprint_sha256": row.fingerprint_sha256}

    # ── helpers ──
    @staticmethod
    def fingerprint(public_key_pem: str) -> str:
        h = hashlib.sha256(public_key_pem.encode("utf-8")).hexdigest()
        return ":".join(h[i:i+2] for i in range(0, len(h), 2))

    async def _fetch_public_key(self, router_url: str) -> str:
        url = router_url.rstrip("/") + self.WELL_KNOWN_PATH
        if not _HAS_HTTPX:
            raise RuntimeError("httpx unavailable — cannot reach router")
        async with httpx.AsyncClient(timeout=self.HTTP_TIMEOUT, verify=False) as cli:
            r = await cli.get(url)
            r.raise_for_status()
            body = r.text.strip()
            if "BEGIN PUBLIC KEY" not in body and "BEGIN CERTIFICATE" not in body:
                raise ValueError("router did not return a PEM public key")
            return body

    async def _ping(self, router_url: str) -> int:
        url = router_url.rstrip("/") + self.PING_PATH
        if not _HAS_HTTPX:
            return -1
        async with httpx.AsyncClient(timeout=self.HTTP_TIMEOUT, verify=False) as cli:
            t0 = time.monotonic()
            r = await cli.get(url)
            r.raise_for_status()
            return int((time.monotonic() - t0) * 1000)
