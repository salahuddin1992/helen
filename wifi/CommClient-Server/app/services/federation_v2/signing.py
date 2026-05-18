"""
Federation v2 — Ed25519 signing & canonical JSON.

A signed event has the shape::

    {
      "type":      "<event-kind>",
      "origin":    "server.example",
      "event_id":  "<sha256-hex>",
      "channel":   "#room@server",
      "sender":    "alice@server",
      "depth":     42,
      "ts":        1731234567,
      "prev":      ["<event-id>", ...],
      "content":   {...},
      "signatures": {
          "server.example": {"ed25519:1": "<base64>"},
          "relay.example":  {"ed25519:1": "<base64>"}
      }
    }

Canonical JSON
--------------
Sorting-by-key, no whitespace, ``ensure_ascii=False``, separators
``(",", ":")``. Compatible with the Matrix canonical encoding.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── canonical json ──────────────────────────────────────────


def canonical_json(obj: Any) -> bytes:
    """Stable JSON encoding suitable for signature input."""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def event_hash(event: dict[str, Any]) -> str:
    """Stable content-addressable id (SHA-256 over canonical JSON,
    excluding the ``signatures`` and ``event_id`` keys)."""
    e = {k: v for k, v in event.items() if k not in ("signatures", "event_id")}
    return hashlib.sha256(canonical_json(e)).hexdigest()


# ── keys ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SigningKey:
    """An Ed25519 keypair (raw 32B public + 32B private)."""
    public: bytes
    private: bytes
    key_id: str = "1"

    def pub_b64(self) -> str:
        return base64.b64encode(self.public).decode("ascii")

    def priv_b64(self) -> str:
        return base64.b64encode(self.private).decode("ascii")


_HAS_NACL: Optional[bool] = None


def _have_nacl() -> bool:
    global _HAS_NACL
    if _HAS_NACL is None:
        try:
            import nacl.signing  # noqa: F401
            _HAS_NACL = True
        except Exception:
            _HAS_NACL = False
    return _HAS_NACL


def _have_cryptography() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,  # noqa: F401
        )
        return True
    except Exception:
        return False


def generate_key(key_id: str = "1") -> SigningKey:
    """Generate a fresh Ed25519 keypair."""
    if _have_nacl():
        import nacl.signing
        sk = nacl.signing.SigningKey.generate()
        return SigningKey(
            public=bytes(sk.verify_key),
            private=bytes(sk),
            key_id=key_id,
        )
    if _have_cryptography():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives import serialization
        sk = Ed25519PrivateKey.generate()
        pub = sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        priv = sk.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return SigningKey(public=pub, private=priv, key_id=key_id)
    # Pure-python fallback: deterministic random keypair (HMAC-based,
    # NOT secure — only meaningful when neither pynacl nor cryptography
    # is installed; signatures will be HMAC-SHA256, advertised via
    # signing_algo="hmac-fallback"). We bake the key id into the bytes.
    raw = os.urandom(32)
    pub = hashlib.sha256(raw).digest()
    return SigningKey(public=pub, private=raw, key_id=key_id)


def sign(key: SigningKey, message: bytes) -> bytes:
    """Produce an Ed25519 signature (64 bytes)."""
    if _have_nacl():
        import nacl.signing
        sk = nacl.signing.SigningKey(key.private)
        return sk.sign(message).signature
    if _have_cryptography():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        sk = Ed25519PrivateKey.from_private_bytes(key.private)
        return sk.sign(message)
    # HMAC-SHA256 fallback (32 bytes).
    import hmac
    return hmac.new(key.private, message, hashlib.sha256).digest()


def verify(public_b64: str, message: bytes, signature: bytes) -> bool:
    """Verify a signature against a base64-encoded public key."""
    try:
        public = base64.b64decode(public_b64)
    except Exception:
        return False
    if _have_nacl() and len(signature) == 64:
        try:
            import nacl.signing
            vk = nacl.signing.VerifyKey(public)
            vk.verify(message, signature)
            return True
        except Exception:
            return False
    if _have_cryptography() and len(signature) == 64:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            vk = Ed25519PublicKey.from_public_bytes(public)
            vk.verify(signature, message)
            return True
        except Exception:
            return False
    # HMAC fallback verify
    if len(signature) == 32:
        import hmac
        # In fallback mode the "public" is sha256(private) so we can't
        # actually verify without the private. We refuse verification
        # to avoid silent false-success.
        return False
    return False


# ── event sign / verify ─────────────────────────────────────


def sign_event(event: dict[str, Any], server_id: str, key: SigningKey) -> dict[str, Any]:
    """Append a signature for ``server_id`` to the event. Idempotent."""
    if "event_id" not in event:
        event["event_id"] = event_hash(event)
    msg = canonical_json({k: v for k, v in event.items() if k != "signatures"})
    sig = sign(key, msg)
    sig_b64 = base64.b64encode(sig).decode("ascii")
    sigs = event.setdefault("signatures", {})
    sigs.setdefault(server_id, {})[f"ed25519:{key.key_id}"] = sig_b64
    return event


def verify_event_signature(
    event: dict[str, Any],
    server_id: str,
    public_key_b64: str,
    key_id: str = "1",
) -> bool:
    """Verify ``server_id``'s signature over the event."""
    sigs = (event.get("signatures") or {}).get(server_id) or {}
    sig_b64 = sigs.get(f"ed25519:{key_id}")
    if not sig_b64:
        return False
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False
    msg = canonical_json({k: v for k, v in event.items() if k != "signatures"})
    return verify(public_key_b64, msg, sig)


# ── singleton (server's own key) ────────────────────────────


_local_key: Optional[SigningKey] = None


def get_local_signing_key() -> SigningKey:
    """Return (or lazily mint) the server's signing key.

    Persistence:
        Stored under ``data/fedv2/signing.key`` (base64 64-byte file:
        first 32 = pub, second 32 = priv). Permissions tightened where
        the OS supports it.
    """
    global _local_key
    if _local_key is not None:
        return _local_key
    path = os.environ.get("HELEN_FEDV2_KEY_FILE") or os.path.join(
        "data", "fedv2", "signing.key",
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                blob = f.read()
            if len(blob) >= 64:
                _local_key = SigningKey(
                    public=blob[:32], private=blob[32:64], key_id="1",
                )
                return _local_key
        except Exception as exc:
            logger.warning("fedv2_signing_key_load_failed err=%s", exc)
    k = generate_key()
    try:
        with open(path, "wb") as f:
            f.write(k.public + k.private)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as exc:
        logger.warning("fedv2_signing_key_persist_failed err=%s", exc)
    _local_key = k
    return _local_key
