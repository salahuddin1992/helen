"""
Ed25519-based license signing for the Tenancy + Billing portal.

Why Ed25519 and not RSA-4096?
-----------------------------
* Signatures are 64 bytes vs ~512 bytes for RSA-4096 — fits easily in a
  HTTP header or QR code if we ever need offline activation.
* Signing/verification is constant-time on every CPU we ship Helen on.
* The ``cryptography`` library exposes a deterministic, side-channel-safe
  implementation that needs no parameter selection.

Key storage
-----------
By default the operator key pair lives in
``HELEN_LICENSE_KEY_DIR`` (defaults to ``data/billing-keys/``).  We
persist:

    operator_ed25519.priv      — PKCS#8 (PEM), 0600 perms
    operator_ed25519.pub       — SubjectPublicKeyInfo (PEM)
    operator_ed25519.fingerprint — first 16 bytes of SHA-256(pub) hex

A new pair is auto-generated on first use. Rotating the key is a manual
operation: delete the files and restart the server — every license
signed under the old key remains verifiable as long as you keep its
``public_key_pem`` field (we embed that into every BillingLicense row).

API
---
* ``LicenseSigner.singleton()`` — process-wide cached instance.
* ``sign_license(payload)``     — returns base64 signature (``str``).
* ``verify_license(payload, sig_b64)`` — bool.
* ``verify_license_with_key(payload, sig_b64, pub_pem)`` — verify with a
  specific public key (useful for legacy licenses).
* ``export_public_key()`` — current public key in PEM form.
* ``export_fingerprint()`` — short fingerprint for UI display.
* ``build_license_payload(...)`` — produce the canonical dict.
* ``serialize_payload(payload)`` — canonical UTF-8 bytes used for hashing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

from app.core.logging import get_logger

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Paths
# ───────────────────────────────────────────────────────────────────────


def _key_dir() -> Path:
    """Resolve the directory holding the operator's signing keys."""
    base = os.getenv("HELEN_LICENSE_KEY_DIR", "data/billing-keys")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


_PRIV_FILE = "operator_ed25519.priv"
_PUB_FILE = "operator_ed25519.pub"
_FP_FILE = "operator_ed25519.fingerprint"


# ───────────────────────────────────────────────────────────────────────
# Canonicalisation
# ───────────────────────────────────────────────────────────────────────


def serialize_payload(payload: dict[str, Any]) -> bytes:
    """Deterministic canonical bytes for signing.

    ``json.dumps`` with sorted keys and tight separators avoids any
    whitespace ambiguity. ``ensure_ascii=False`` lets Arabic / Unicode
    plan names round-trip without escaping.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(payload: dict[str, Any]) -> str:
    return hashlib.sha256(serialize_payload(payload)).hexdigest()


def build_license_payload(
    *,
    license_key: str,
    workspace_id: str,
    plan_slug: str,
    seats: int,
    features: dict[str, Any] | None,
    issued_at: datetime,
    expires_at: datetime,
    issuer: str = "helen-operator",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical payload that gets signed.  Field order is
    deterministic via the serializer's ``sort_keys=True`` so we never
    need to repeat the keys here in any particular order."""
    return {
        "v": 1,
        "key": license_key,
        "workspace_id": workspace_id,
        "plan": plan_slug,
        "seats": int(max(1, seats)),
        "features": features or {},
        "issued_at": issued_at.astimezone(timezone.utc).isoformat(),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "issuer": issuer,
        "metadata": metadata or {},
    }


# ───────────────────────────────────────────────────────────────────────
# LicenseSigner
# ───────────────────────────────────────────────────────────────────────


class LicenseSigner:
    """Ed25519 signing wrapper.

    Thread-safe: the cryptography primitives themselves don't need a
    lock, but we serialise file IO during key generation so two startup
    races don't write half-formed key files.
    """

    _singleton: "LicenseSigner | None" = None
    _singleton_lock = threading.Lock()

    def __init__(
        self,
        *,
        priv_path: Optional[Path] = None,
        pub_path: Optional[Path] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._priv_path = priv_path or (_key_dir() / _PRIV_FILE)
        self._pub_path = pub_path or (_key_dir() / _PUB_FILE)
        self._fp_path = self._priv_path.parent / _FP_FILE
        self._priv: Optional[Ed25519PrivateKey] = None
        self._pub: Optional[Ed25519PublicKey] = None
        self._fingerprint: str = ""
        self._load_or_generate()

    # ── singleton ─────────────────────────────────────────────────
    @classmethod
    def singleton(cls) -> "LicenseSigner":
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the singleton — used in test fixtures that swap
        ``HELEN_LICENSE_KEY_DIR`` to a temp path."""
        with cls._singleton_lock:
            cls._singleton = None

    # ── key lifecycle ─────────────────────────────────────────────
    def _load_or_generate(self) -> None:
        with self._lock:
            if self._priv_path.exists() and self._pub_path.exists():
                try:
                    self._priv = self._load_priv()
                    self._pub = self._load_pub()
                    self._fingerprint = self._compute_fingerprint()
                    return
                except Exception as e:                                # noqa: BLE001
                    logger.error("license-signer: load failed (%s); regenerating", e)
            self._generate_and_save()

    def _load_priv(self) -> Ed25519PrivateKey:
        data = self._priv_path.read_bytes()
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError("operator private key is not Ed25519")
        return key

    def _load_pub(self) -> Ed25519PublicKey:
        data = self._pub_path.read_bytes()
        key = serialization.load_pem_public_key(data)
        if not isinstance(key, Ed25519PublicKey):
            raise RuntimeError("operator public key is not Ed25519")
        return key

    def _generate_and_save(self) -> None:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()

        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        self._priv_path.write_bytes(priv_pem)
        self._pub_path.write_bytes(pub_pem)
        try:
            os.chmod(self._priv_path, 0o600)
        except OSError:                                                # pragma: no cover
            # Windows file ACLs differ — fall back to default permissions.
            pass

        self._priv = priv
        self._pub = pub
        self._fingerprint = self._compute_fingerprint()
        self._fp_path.write_text(self._fingerprint, encoding="ascii")
        logger.info(
            "license-signer: generated new Ed25519 keypair fingerprint=%s",
            self._fingerprint,
        )

    def _compute_fingerprint(self) -> str:
        assert self._pub is not None
        raw = self._pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return hashlib.sha256(raw).hexdigest()[:32]

    # ── public API ────────────────────────────────────────────────
    def sign_license(self, payload: dict[str, Any]) -> str:
        """Return the base64-encoded Ed25519 signature of the canonical
        payload bytes."""
        if self._priv is None:                                        # pragma: no cover
            raise RuntimeError("license-signer: private key not loaded")
        msg = serialize_payload(payload)
        sig = self._priv.sign(msg)
        return base64.b64encode(sig).decode("ascii")

    def verify_license(self, payload: dict[str, Any], sig_b64: str) -> bool:
        """Verify against the current operator public key."""
        return self._verify(self._pub, payload, sig_b64)

    def verify_license_with_key(
        self,
        payload: dict[str, Any],
        sig_b64: str,
        public_pem: str,
    ) -> bool:
        """Verify against an arbitrary public key (e.g. one stored in
        the license row from a previous key rotation)."""
        try:
            key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        except Exception:                                              # noqa: BLE001
            return False
        if not isinstance(key, Ed25519PublicKey):
            return False
        return self._verify(key, payload, sig_b64)

    def _verify(
        self,
        pub: Optional[Ed25519PublicKey],
        payload: dict[str, Any],
        sig_b64: str,
    ) -> bool:
        if pub is None:
            return False
        try:
            sig = base64.b64decode(sig_b64.encode("ascii"))
        except Exception:                                              # noqa: BLE001
            return False
        try:
            pub.verify(sig, serialize_payload(payload))
            return True
        except InvalidSignature:
            return False
        except Exception:                                              # noqa: BLE001
            return False

    def export_public_key(self) -> str:
        """Operator's current public key in PEM form."""
        if self._pub is None:                                          # pragma: no cover
            raise RuntimeError("license-signer: public key not loaded")
        return self._pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def export_fingerprint(self) -> str:
        return self._fingerprint


# Convenience singletons -------------------------------------------------


def get_signer() -> LicenseSigner:
    """Return the lazily-initialised process-wide signer."""
    return LicenseSigner.singleton()
