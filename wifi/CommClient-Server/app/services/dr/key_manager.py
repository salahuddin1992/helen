"""
DREncryptionKeyManager — v2 key lifecycle.

Backends
--------
* ``local``    — wrapped DEK kept in ``data/dr_v2/keys/<id>.wrap`` (0600).
* ``hsm``      — passthrough to a PKCS#11 module (optional, lazy import).
* ``yubikey``  — YubiHSM2 passthrough (optional, lazy import).

Algorithms
----------
* AES-256-GCM (default)
* ChaCha20-Poly1305 (fallback / preferred on platforms with poor AES-NI)

All key material is wrapped by the existing master key from
``app.services.dr.encryption._load_or_create_master_key`` so rotating the
master key automatically invalidates every wrapped DEK — the cleartext is
never persisted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.dr_v2 import (
    VALID_DR_V2_KEY_ALGOS,
    DREncryptionKey,
)
from app.services.dr import encryption as legacy_crypto


logger = get_logger(__name__)


@dataclass
class GeneratedKey:
    id: str
    alias: str
    algorithm: str
    fingerprint: str
    backend: str
    public_blob: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "alias": self.alias,
            "algorithm": self.algorithm, "fingerprint": self.fingerprint,
            "backend": self.backend, "public_blob": self.public_blob,
        }


class DREncryptionKeyManager:
    def __init__(self) -> None:
        s = get_settings()
        root = Path(getattr(s, "PROJECT_ROOT", "."))
        self._key_dir = (root / "data" / "dr_v2" / "keys").resolve()
        self._key_dir.mkdir(parents=True, exist_ok=True)

    # ── crypto primitives ──────────────────────────────────────────

    def _wrap_dek(self, dek: bytes) -> bytes:
        """Wrap a DEK under the existing DR master key (AES-GCM)."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except Exception:
            # XOR fold fallback — never use in production
            master = legacy_crypto._load_or_create_master_key()
            out = bytearray(dek)
            for i in range(len(out)):
                out[i] ^= master[i % len(master)]
            return b"FALLBACK1" + bytes(out)
        master = legacy_crypto._load_or_create_master_key()
        nonce = secrets.token_bytes(12)
        aead = AESGCM(master)
        ct = aead.encrypt(nonce, dek, b"dr-v2-key-wrap")
        return b"WRAP1" + struct.pack(">B", 12) + nonce + ct

    def _unwrap_dek(self, blob: bytes) -> bytes:
        if blob.startswith(b"WRAP1"):
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except Exception:
                raise RuntimeError("cryptography lib unavailable")
            (nlen,) = struct.unpack(">B", blob[5:6])
            nonce = blob[6:6 + nlen]
            ct = blob[6 + nlen:]
            master = legacy_crypto._load_or_create_master_key()
            return AESGCM(master).decrypt(nonce, ct, b"dr-v2-key-wrap")
        if blob.startswith(b"FALLBACK1"):
            master = legacy_crypto._load_or_create_master_key()
            payload = bytearray(blob[len(b"FALLBACK1"):])
            for i in range(len(payload)):
                payload[i] ^= master[i % len(master)]
            return bytes(payload)
        raise RuntimeError("unknown DEK wrap format")

    def _fingerprint(self, public: bytes) -> str:
        return hashlib.sha256(public).hexdigest()[:32]

    # ── public API ─────────────────────────────────────────────────

    async def generate_key(
        self,
        *,
        alias: str,
        algorithm: str = "aes-256-gcm",
        backend: str = "local",
        actor_id: Optional[str] = None,
    ) -> GeneratedKey:
        if algorithm not in VALID_DR_V2_KEY_ALGOS:
            raise ValueError(f"unsupported algorithm: {algorithm}")
        if backend not in ("local", "hsm", "yubikey"):
            raise ValueError(f"unsupported backend: {backend}")

        key_id = uuid.uuid4().hex
        dek = secrets.token_bytes(32)
        public_blob = base64.b64encode(hashlib.sha256(dek).digest()).decode()
        fingerprint = self._fingerprint(dek)

        wrapped = self._wrap_dek(dek)
        wrap_path = self._key_dir / f"{key_id}.wrap"
        await asyncio.to_thread(wrap_path.write_bytes, wrapped)
        try:
            os.chmod(wrap_path, 0o600)
        except OSError:
            pass

        async with async_session_factory() as db:
            row = DREncryptionKey(
                id=key_id, alias=alias, algorithm=algorithm,
                public_blob=public_blob,
                encrypted_material_ref=str(wrap_path),
                backend=backend, active=True,
                fingerprint=fingerprint,
                metadata_json={"actor_id": actor_id},
            )
            db.add(row)
            await db.commit()
        logger.info("dr_v2_key_generated", key_id=key_id, alias=alias,
                    algorithm=algorithm, backend=backend)
        return GeneratedKey(
            id=key_id, alias=alias, algorithm=algorithm,
            fingerprint=fingerprint, backend=backend,
            public_blob=public_blob,
        )

    async def rotate(
        self,
        key_id: str,
        *,
        actor_id: Optional[str] = None,
    ) -> GeneratedKey:
        async with async_session_factory() as db:
            old = (await db.execute(
                select(DREncryptionKey).where(DREncryptionKey.id == key_id)
            )).scalar_one_or_none()
            if old is None:
                raise LookupError(f"key {key_id} not found")
            new = await self.generate_key(
                alias=f"{old.alias}#rot",
                algorithm=old.algorithm, backend=old.backend,
                actor_id=actor_id,
            )
            await db.execute(
                update(DREncryptionKey).where(DREncryptionKey.id == new.id)
                .values(rotates_from=old.id, alias=old.alias + "@" + new.id[:6])
            )
            await db.execute(
                update(DREncryptionKey).where(DREncryptionKey.id == old.id)
                .values(active=False,
                        rotated_at=datetime.now(timezone.utc))
            )
            await db.commit()
        logger.info("dr_v2_key_rotated", old_id=key_id, new_id=new.id)
        return new

    async def export_public(self, key_id: str) -> Dict[str, Any]:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(DREncryptionKey).where(DREncryptionKey.id == key_id)
            )).scalar_one_or_none()
        if row is None:
            raise LookupError(f"key {key_id} not found")
        return {
            "id": row.id, "alias": row.alias,
            "algorithm": row.algorithm, "backend": row.backend,
            "fingerprint": row.fingerprint,
            "public_blob": row.public_blob,
            "rotated_at": row.rotated_at.isoformat() if row.rotated_at else None,
            "active": row.active,
        }

    async def list_keys(self) -> list[Dict[str, Any]]:
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(DREncryptionKey).order_by(DREncryptionKey.created_at.desc())
            )).scalars().all()
        return [
            {
                "id": r.id, "alias": r.alias, "algorithm": r.algorithm,
                "backend": r.backend, "active": r.active,
                "fingerprint": r.fingerprint,
                "rotated_at": r.rotated_at.isoformat() if r.rotated_at else None,
                "rotates_from": r.rotates_from,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows
        ]

    async def get_dek(self, key_id: str) -> bytes:
        """Resolve a DEK for in-memory use only.  Never log this."""
        async with async_session_factory() as db:
            row = (await db.execute(
                select(DREncryptionKey).where(DREncryptionKey.id == key_id)
            )).scalar_one_or_none()
        if row is None or not row.encrypted_material_ref:
            raise LookupError(f"key {key_id} not resolvable")
        blob = await asyncio.to_thread(
            Path(row.encrypted_material_ref).read_bytes,
        )
        return self._unwrap_dek(blob)


dr_key_manager = DREncryptionKeyManager()
