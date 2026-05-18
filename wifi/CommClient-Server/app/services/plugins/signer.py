"""
Plugin code signing & verification (Ed25519).

Helen ships with one official signing pubkey baked into env config:

    HELEN_PLUGINS_OFFICIAL_PUBKEY = base64-encoded raw 32-byte Ed25519
                                     public key.

Additional trusted keys may be added by admins via
``POST /api/admin/plugins/trusted-keys``; they're stored on disk under
``data/plugin-trusted-keys.json``.

If ``cryptography`` is unavailable we degrade to "signature absent
allowed" — the loader will warn but still install community plugins.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


try:                                                                  # pragma: no cover
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    Ed25519PrivateKey = None                                          # type: ignore
    Ed25519PublicKey = None                                           # type: ignore
    InvalidSignature = Exception                                       # type: ignore
    _CRYPTO_AVAILABLE = False


TRUSTED_KEYS_PATH = Path(
    os.getenv("HELEN_PLUGIN_TRUSTED_KEYS", "data/plugin-trusted-keys.json"),
)
TRUSTED_KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)


# ───────────────────────────────────────────────────────────────────────
# Persistence
# ───────────────────────────────────────────────────────────────────────


def _load_trusted_keys() -> dict[str, str]:
    if not TRUSTED_KEYS_PATH.exists():
        return {}
    try:
        return json.loads(TRUSTED_KEYS_PATH.read_text(encoding="utf-8"))
    except Exception as e:                                              # noqa: BLE001
        logger.warning("plugin.keys: failed to read trust store: %s", e)
        return {}


def _save_trusted_keys(keys: dict[str, str]) -> None:
    TRUSTED_KEYS_PATH.write_text(
        json.dumps(keys, indent=2, sort_keys=True), encoding="utf-8",
    )


def list_trusted_keys() -> dict[str, str]:
    keys = _load_trusted_keys()
    official = os.getenv("HELEN_PLUGINS_OFFICIAL_PUBKEY")
    if official:
        keys["helen-official"] = official
    return keys


def add_trusted_key(name: str, pubkey_b64: str) -> None:
    keys = _load_trusted_keys()
    keys[name] = pubkey_b64
    _save_trusted_keys(keys)


def remove_trusted_key(name: str) -> None:
    keys = _load_trusted_keys()
    keys.pop(name, None)
    _save_trusted_keys(keys)


# ───────────────────────────────────────────────────────────────────────
# Signing / verifying
# ───────────────────────────────────────────────────────────────────────


def generate_keypair() -> tuple[str, str]:
    """Returns ``(privkey_b64, pubkey_b64)``."""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography not available")
    priv = Ed25519PrivateKey.generate()                                # type: ignore[union-attr]
    raw_priv = priv.private_bytes_raw()                                 # type: ignore[union-attr]
    raw_pub = priv.public_key().public_bytes_raw()                      # type: ignore[union-attr]
    return base64.b64encode(raw_priv).decode(), base64.b64encode(raw_pub).decode()


def sign_payload(payload: bytes, privkey_b64: str) -> str:
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography not available")
    priv = Ed25519PrivateKey.from_private_bytes(                       # type: ignore[union-attr]
        base64.b64decode(privkey_b64),
    )
    sig = priv.sign(payload)
    return base64.b64encode(sig).decode()


def verify_signature(
    payload: bytes, signature_b64: str, pubkey_b64: str,
) -> bool:
    if not _CRYPTO_AVAILABLE:
        logger.warning("plugin.sig: cryptography missing — cannot verify")
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(                      # type: ignore[union-attr]
            base64.b64decode(pubkey_b64),
        )
        pub.verify(base64.b64decode(signature_b64), payload)
        return True
    except InvalidSignature:                                            # type: ignore[misc]
        return False
    except Exception as e:                                              # noqa: BLE001
        logger.warning("plugin.sig.verify error: %s", e)
        return False


def verify_against_trust_store(
    payload: bytes, signature_b64: str, signed_by: Optional[str],
) -> bool:
    """If ``signed_by`` is set, verify against that exact trusted key;
    otherwise try every key in the trust store."""
    if not signature_b64:
        return False
    keys = list_trusted_keys()
    if signed_by:
        pk = keys.get(signed_by)
        return verify_signature(payload, signature_b64, pk) if pk else False
    return any(
        verify_signature(payload, signature_b64, pk) for pk in keys.values()
    )
