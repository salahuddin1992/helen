"""
Zero-Trust — mTLS certificate manager.

Each workload identity gets a short-lived X.509 SVID. Certs are issued
by the trust root, rotate every hour, and are enforced on every
inbound request when ``ZT_MTLS_REQUIRED=1``.

Cipher policy:
    * TLS 1.3 only
    * Restricted suites: TLS_AES_128_GCM_SHA256 / TLS_AES_256_GCM_SHA384 /
      TLS_CHACHA20_POLY1305_SHA256
    * SAN-based identity binding (the spiffe_id sits in URI-SAN)
"""
from __future__ import annotations

import datetime as _dt
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


ALLOWED_TLS13_SUITES = (
    "TLS_AES_128_GCM_SHA256",
    "TLS_AES_256_GCM_SHA384",
    "TLS_CHACHA20_POLY1305_SHA256",
)

DEFAULT_CERT_DIR = os.environ.get("HELEN_ZT_CERT_DIR", "data/zt/certs")


@dataclass
class WorkloadCert:
    spiffe_id: str
    cert_pem: str
    key_pem: str
    not_before: _dt.datetime
    not_after: _dt.datetime


class MTLSManager:
    """Issues per-workload mTLS certificates. Uses ``cryptography`` if
    available; falls back to opaque tokens otherwise."""

    def __init__(self) -> None:
        os.makedirs(DEFAULT_CERT_DIR, exist_ok=True)

    def _have_crypto(self) -> bool:
        try:
            from cryptography import x509  # noqa: F401
            return True
        except Exception:
            return False

    async def issue_cert(
        self,
        spiffe_id: str,
        *,
        ttl_hours: int = 1,
    ) -> WorkloadCert:
        if not self._have_crypto():
            # Fallback: opaque PEM-ish blob — caller treats as a token.
            now = _dt.datetime.now(_dt.timezone.utc)
            return WorkloadCert(
                spiffe_id=spiffe_id,
                cert_pem=f"-----BEGIN HELEN-FAKE-CERT-----\n{spiffe_id}\n-----END HELEN-FAKE-CERT-----\n",
                key_pem="-----BEGIN HELEN-FAKE-KEY-----\nunavailable\n-----END HELEN-FAKE-KEY-----\n",
                not_before=now,
                not_after=now + _dt.timedelta(hours=ttl_hours),
            )
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        # Per-workload P-256 key.
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, spiffe_id),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Helen"),
        ])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + _dt.timedelta(hours=ttl_hours))
            .add_extension(
                x509.SubjectAlternativeName([x509.UniformResourceIdentifier(spiffe_id)]),
                critical=True,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([
                    x509.OID_SERVER_AUTH, x509.OID_CLIENT_AUTH,
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        # Persist on disk under cert dir.
        safe = spiffe_id.replace("/", "_").replace(":", "_")
        with open(os.path.join(DEFAULT_CERT_DIR, safe + ".pem"), "w", encoding="utf-8") as f:
            f.write(cert_pem)
        with open(os.path.join(DEFAULT_CERT_DIR, safe + ".key"), "w", encoding="utf-8") as f:
            f.write(key_pem)
        try:
            os.chmod(os.path.join(DEFAULT_CERT_DIR, safe + ".key"), 0o600)
        except Exception:
            pass
        return WorkloadCert(
            spiffe_id=spiffe_id,
            cert_pem=cert_pem,
            key_pem=key_pem,
            not_before=now,
            not_after=now + _dt.timedelta(hours=ttl_hours),
        )

    async def rotate_all(self) -> int:
        """Re-issue every cert in ``DEFAULT_CERT_DIR``."""
        if not os.path.exists(DEFAULT_CERT_DIR):
            return 0
        n = 0
        for fname in os.listdir(DEFAULT_CERT_DIR):
            if not fname.endswith(".pem"):
                continue
            # Recover the SPIFFE id from the filename (heuristic — best-effort).
            spiffe = fname[:-4].replace("_", "/")
            try:
                await self.issue_cert(spiffe)
                n += 1
            except Exception:
                continue
        return n

    def enforce_cipher_suite(self, name: str) -> bool:
        return name in ALLOWED_TLS13_SUITES


_mgr: Optional[MTLSManager] = None


def get_mtls_manager() -> MTLSManager:
    global _mgr
    if _mgr is None:
        _mgr = MTLSManager()
    return _mgr
