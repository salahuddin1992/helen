"""
OnboardingCertManager — generates / imports / inspects TLS material.

Uses the ``cryptography`` library. All persistence goes through the
``system_certs`` table; the private key is encrypted at rest using a
key derived from ``settings.SECRET_KEY`` (HKDF + AES-GCM envelope).

Supported key types: RSA-4096, Ed25519, ECDSA-P256.
"""
from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.system_cert import SystemCert

logger = get_logger(__name__)


# Lazy import to keep startup fast and avoid hard dep at import time.
def _crypto():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, ed25519, ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return {
        "x509": x509, "NameOID": NameOID, "hashes": hashes,
        "serialization": serialization, "rsa": rsa, "ed25519": ed25519,
        "ec": ec, "AESGCM": AESGCM, "HKDF": HKDF,
    }


class OnboardingCertManager:
    """Async certificate manager."""

    KEY_SALT = b"helen-onboarding-cert-v1"

    # ── envelope crypto ─────────────────────────────────
    def _envelope_key(self) -> bytes:
        from app.core.config import get_settings
        c = _crypto()
        master = str(get_settings().SECRET_KEY).encode("utf-8") or b"helen-default"
        return c["HKDF"](
            algorithm=c["hashes"].SHA256(),
            length=32,
            salt=self.KEY_SALT,
            info=b"system-cert-key",
        ).derive(master)

    def _encrypt_key(self, key_pem: bytes) -> str:
        c = _crypto()
        gcm = c["AESGCM"](self._envelope_key())
        nonce = os.urandom(12)
        ct = gcm.encrypt(nonce, key_pem, None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def _decrypt_key(self, blob: str) -> bytes:
        c = _crypto()
        raw = base64.b64decode(blob)
        nonce, ct = raw[:12], raw[12:]
        gcm = c["AESGCM"](self._envelope_key())
        return gcm.decrypt(nonce, ct, None)

    # ── generation ──────────────────────────────────────
    async def generate_self_signed(
        self,
        db: AsyncSession,
        *,
        cn: str,
        san: list[str],
        duration_days: int = 825,
        key_type: str = "rsa",
        actor: str = "system",
    ) -> dict[str, Any]:
        c = _crypto()
        x509 = c["x509"]
        NameOID = c["NameOID"]
        serialization = c["serialization"]
        hashes = c["hashes"]

        # ── key ──
        if key_type == "ed25519":
            private_key = c["ed25519"].Ed25519PrivateKey.generate()
            sign_hash = None  # Ed25519 signs without a hash arg
        elif key_type == "ecdsa":
            private_key = c["ec"].generate_private_key(c["ec"].SECP256R1())
            sign_hash = hashes.SHA256()
        else:
            private_key = c["rsa"].generate_private_key(
                public_exponent=65537, key_size=4096,
            )
            sign_hash = hashes.SHA256()

        public_key = private_key.public_key()
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Helen"),
        ])
        now = datetime.now(timezone.utc)
        not_after = now + timedelta(days=int(duration_days))

        san_objects = [x509.DNSName(s) for s in san if s]
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, key_encipherment=True,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, crl_sign=True, encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        if san_objects:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(san_objects),
                critical=False,
            )

        cert = builder.sign(private_key=private_key, algorithm=sign_hash)

        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        if isinstance(private_key, c["ed25519"].Ed25519PrivateKey):
            key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        else:
            key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )

        return await self._persist(
            db, cert_pem=cert_pem, key_pem=key_pem.decode("ascii"),
            key_type=key_type, is_self_signed=True, actor=actor,
        )

    # ── import ──────────────────────────────────────────
    async def import_cert(
        self, db: AsyncSession, *,
        cert_pem: str, key_pem: str, actor: str = "system",
    ) -> dict[str, Any]:
        c = _crypto()
        # Validate parses
        cert = c["x509"].load_pem_x509_certificate(cert_pem.encode("ascii"))
        c["serialization"].load_pem_private_key(key_pem.encode("ascii"), password=None)
        # Determine key type
        pk = c["serialization"].load_pem_private_key(key_pem.encode("ascii"), password=None)
        key_type = self._detect_key_type(pk, c)
        return await self._persist(
            db, cert_pem=cert_pem, key_pem=key_pem,
            key_type=key_type, is_self_signed=False, actor=actor,
            cert_obj=cert,
        )

    def _detect_key_type(self, pk: Any, c: dict) -> str:
        from cryptography.hazmat.primitives.asymmetric import rsa, ed25519, ec
        if isinstance(pk, ed25519.Ed25519PrivateKey):
            return "ed25519"
        if isinstance(pk, ec.EllipticCurvePrivateKey):
            return "ecdsa"
        if isinstance(pk, rsa.RSAPrivateKey):
            return "rsa"
        return "unknown"

    # ── persistence ────────────────────────────────────
    async def _persist(
        self, db: AsyncSession, *,
        cert_pem: str, key_pem: str, key_type: str,
        is_self_signed: bool, actor: str, cert_obj: Any = None,
    ) -> dict[str, Any]:
        c = _crypto()
        if cert_obj is None:
            cert_obj = c["x509"].load_pem_x509_certificate(cert_pem.encode("ascii"))

        # Extract metadata
        cn = ""
        try:
            cn = cert_obj.subject.get_attributes_for_oid(c["NameOID"].COMMON_NAME)[0].value
        except Exception:
            pass
        san_list: list[str] = []
        try:
            ext = cert_obj.extensions.get_extension_for_class(c["x509"].SubjectAlternativeName)
            san_list = [v for v in ext.value.get_values_for_type(c["x509"].DNSName)]
        except Exception:
            pass
        fingerprint = ":".join(
            f"{b:02x}" for b in cert_obj.fingerprint(c["hashes"].SHA256())
        )
        serial = format(cert_obj.serial_number, "x")
        not_before = cert_obj.not_valid_before.replace(tzinfo=timezone.utc) \
            if cert_obj.not_valid_before.tzinfo is None else cert_obj.not_valid_before
        not_after = cert_obj.not_valid_after.replace(tzinfo=timezone.utc) \
            if cert_obj.not_valid_after.tzinfo is None else cert_obj.not_valid_after

        # Deactivate previous server certs.
        await db.execute(
            update(SystemCert).where(SystemCert.role == "server").values(active=False)
        )

        row = SystemCert(
            role="server",
            key_type=key_type,
            common_name=cn or "unknown",
            san_list=san_list,
            fingerprint_sha256=fingerprint,
            serial_number=serial,
            not_before=not_before,
            not_after=not_after,
            cert_pem=cert_pem,
            key_pem_encrypted=self._encrypt_key(key_pem.encode("ascii")),
            is_self_signed=is_self_signed,
            active=True,
        )
        db.add(row)
        await db.flush()

        # Mirror to disk best-effort so the running server can pick it up
        # on next restart. (Live reload is out-of-scope here.)
        try:
            self._write_to_disk(cert_pem, key_pem)
        except Exception as e:
            logger.warning("cert_disk_mirror_failed", error=str(e))

        return row.to_dict()

    def _write_to_disk(self, cert_pem: str, key_pem: str) -> None:
        from app.core.config import get_settings
        from pathlib import Path
        s = get_settings()
        root = Path(s.PROJECT_ROOT) if hasattr(s, "PROJECT_ROOT") else Path.cwd()
        cert_dir = root / "data" / "certs"
        cert_dir.mkdir(parents=True, exist_ok=True)
        (cert_dir / "helen.crt").write_text(cert_pem, encoding="utf-8")
        (cert_dir / "helen.key").write_text(key_pem, encoding="utf-8")

    # ── info & download ────────────────────────────────
    async def get_info(self, db: AsyncSession) -> dict[str, Any]:
        row = (await db.execute(
            select(SystemCert).where(SystemCert.role == "server",
                                     SystemCert.active.is_(True))
            .order_by(SystemCert.created_at.desc())
        )).scalars().first()
        if row is None:
            return {"present": False}
        return {"present": True, **row.to_dict()}

    async def download_root(self, db: AsyncSession) -> bytes:
        row = (await db.execute(
            select(SystemCert)
            .where(SystemCert.role.in_(["root", "server"]),
                   SystemCert.active.is_(True))
            .order_by(SystemCert.role.desc(), SystemCert.created_at.desc())
        )).scalars().first()
        if row is None:
            return b""
        return row.cert_pem.encode("ascii")

    @staticmethod
    def fingerprint(cert_pem: str) -> str:
        h = hashlib.sha256(cert_pem.encode("ascii")).hexdigest()
        return ":".join(h[i:i+2] for i in range(0, len(h), 2))
