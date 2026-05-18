"""Backup encryption — AES-256-GCM for backup files.

Backups today are plain SQLite copies — anyone with disk access
can read them. This module wraps a backup file with AES-256-GCM
authenticated encryption using a key derived (HKDF-SHA256) from
``COMMCLIENT_CLUSTER_ID`` (or a pinned ``HELEN_BACKUP_KEY`` env).

Format on disk (``*.enc`` extension):

    [16 bytes nonce][encrypted body][16 bytes tag]

The HKDF salt is fixed (``helen-backup-v1``) so re-derivation is
deterministic per cluster. The actual encryption key never touches
disk; it lives only in process memory while the backup runs.

Falls back gracefully to a no-op (cleartext) when ``cryptography``
isn't installed — operators who don't need encryption don't pay
the dependency cost.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_HKDF_SALT = b"helen-backup-v1"
_HKDF_INFO = b"helen-backup-encryption"


def _derive_key() -> bytes:
    """Returns 32 bytes for AES-256."""
    raw = os.environ.get("HELEN_BACKUP_KEY") or ""
    if raw:
        secret = raw.encode()
    else:
        try:
            from app.core.config import get_settings
            secret = (get_settings().COMMCLIENT_CLUSTER_ID or "default").encode()
        except Exception:
            secret = b"default"

    # HKDF-SHA256 (extract + expand to 32 bytes).
    prk = hmac.new(_HKDF_SALT, secret, hashlib.sha256).digest()
    okm = hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()
    return okm[:32]


def _aesgcm_available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        return True
    except ImportError:
        return False


def encrypt_file(in_path: str, out_path: str) -> dict:
    """Encrypt ``in_path`` → ``out_path``. Returns a stats dict.

    On systems without ``cryptography``, copies the file unmodified
    and reports ``encrypted=False`` so the caller can decide what to
    do.
    """
    src = Path(in_path)
    dst = Path(out_path)
    if not src.is_file():
        return {"ok": False, "error": "input_not_found"}

    if not _aesgcm_available():
        try:
            dst.write_bytes(src.read_bytes())
            return {"ok": True, "encrypted": False,
                    "reason": "cryptography_not_installed",
                    "size_bytes": dst.stat().st_size}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return {"ok": False, "error": "import_failed"}

    try:
        key = _derive_key()
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(16)
        plaintext = src.read_bytes()
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        # AESGCM.encrypt already includes the 16-byte tag at the end.
        dst.write_bytes(nonce + ciphertext)
        return {
            "ok":           True,
            "encrypted":    True,
            "size_in":      len(plaintext),
            "size_out":     dst.stat().st_size,
            "nonce_hex":    nonce.hex(),
        }
    except Exception as e:
        logger.warning("backup_encrypt_failed", error=str(e))
        return {"ok": False, "error": str(e)[:120]}


def decrypt_file(in_path: str, out_path: str) -> dict:
    """Decrypt ``in_path`` → ``out_path``."""
    src = Path(in_path)
    dst = Path(out_path)
    if not src.is_file():
        return {"ok": False, "error": "input_not_found"}

    if not _aesgcm_available():
        try:
            dst.write_bytes(src.read_bytes())
            return {"ok": True, "encrypted": False,
                    "reason": "cryptography_not_installed"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return {"ok": False, "error": "import_failed"}

    try:
        key = _derive_key()
        aesgcm = AESGCM(key)
        blob = src.read_bytes()
        if len(blob) < 32:
            return {"ok": False, "error": "blob_too_small"}
        nonce, ciphertext = blob[:16], blob[16:]
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        dst.write_bytes(plaintext)
        return {
            "ok":         True,
            "decrypted":  True,
            "size_in":    len(blob),
            "size_out":   len(plaintext),
        }
    except Exception as e:
        logger.warning("backup_decrypt_failed", error=str(e)[:120])
        return {"ok": False, "error": "decrypt_failed"}


def status() -> dict:
    return {
        "available":         _aesgcm_available(),
        "key_source":        ("HELEN_BACKUP_KEY env"
                              if os.environ.get("HELEN_BACKUP_KEY")
                              else "cluster_id derivation"),
        "key_fingerprint":   hashlib.sha256(_derive_key()).hexdigest()[:16],
    }
