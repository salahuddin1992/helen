"""
Streaming AES-256-GCM envelope encryption for DR backups.

Layout on disk
--------------
    [ MAGIC: b"HDR1" ][ 1B version ][ 12B nonce ][ 16B key_id ]
    [ N x ( 4B big-endian chunk_len | ciphertext_chunk_with_tag ) ]

The per-backup data key is derived from the master key + per-backup nonce
using HKDF-SHA256.  The master key is resolved in this order:

    1. ``app.services.secret_store`` (if available — Phase 1 Module B)
    2. ``HELEN_DR_MASTER_KEY`` env var (base64 raw bytes)
    3. ``<PROJECT_ROOT>/data/dr/master.key`` (auto-generated, 0600 perms)

``cryptography`` is the only hard dependency; if it is missing, the engine
falls back to plain gzip and logs a warning.  This is intentional — losing
a backup is worse than losing encryption.
"""
from __future__ import annotations

import base64
import gzip
import os
import secrets as pysecrets
import struct
from pathlib import Path
from typing import IO, Iterator, Optional, Tuple

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── optional dep ─────────────────────────────────────────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes as _crypto_hashes
    _CRYPTO_OK = True
except Exception:                                                      # pragma: no cover
    AESGCM = None                       # type: ignore[assignment]
    HKDF = None                         # type: ignore[assignment]
    _crypto_hashes = None               # type: ignore[assignment]
    _CRYPTO_OK = False


_MAGIC = b"HDR1"
_VERSION = 1
_CHUNK = 64 * 1024   # 64 KiB read window


# ── master-key resolution ───────────────────────────────────────


def _master_key_path() -> Path:
    s = get_settings()
    root = Path(getattr(s, "PROJECT_ROOT", "."))
    p = root / "data" / "dr" / "master.key"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_or_create_master_key() -> bytes:
    # 1) Phase-1 secret store
    try:
        from app.services.secret_store import secret_store        # type: ignore
        val = secret_store.get("dr.master_key")
        if val:
            return base64.b64decode(val) if isinstance(val, str) else bytes(val)
    except Exception:
        pass

    # 2) env
    env = os.environ.get("HELEN_DR_MASTER_KEY")
    if env:
        try:
            return base64.b64decode(env)
        except Exception:
            return env.encode()

    # 3) local file (auto-generated, persisted)
    p = _master_key_path()
    if p.exists():
        try:
            data = p.read_bytes()
            if len(data) >= 32:
                return data[:32]
        except OSError:
            pass

    key = pysecrets.token_bytes(32)
    try:
        p.write_bytes(key)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as e:
        logger.warning("dr_master_key_write_failed", error=str(e))
    return key


def _derive_data_key(master: bytes, nonce: bytes, key_id: bytes) -> bytes:
    if not _CRYPTO_OK:
        # fallback: simple xor-fold — only reached when cryptography lib is absent
        out = bytearray(32)
        material = master + nonce + key_id
        for i, b in enumerate(material):
            out[i % 32] ^= b
        return bytes(out)
    hk = HKDF(
        algorithm=_crypto_hashes.SHA256(), length=32,
        salt=nonce, info=b"helen-dr-aead" + key_id,
    )
    return hk.derive(master)


# ── streaming engine ────────────────────────────────────────────


def crypto_available() -> bool:
    return _CRYPTO_OK


def encrypt_stream(src: IO[bytes], dst: IO[bytes], encrypt: bool = True) -> Tuple[bool, Optional[str]]:
    """Stream ``src`` → ``dst`` with optional AES-256-GCM + gzip envelope.

    Returns ``(encrypted, key_id_hex)``.  ``key_id`` is a 16-byte random tag
    used only to identify which derived key was used — it is NOT a secret.
    """
    if not encrypt or not _CRYPTO_OK:
        if encrypt and not _CRYPTO_OK:
            logger.warning("dr_encryption_unavailable_falling_back_to_gzip_only")
        with gzip.GzipFile(fileobj=dst, mode="wb", compresslevel=6) as gz:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                gz.write(chunk)
        return False, None

    master = _load_or_create_master_key()
    nonce = pysecrets.token_bytes(12)
    key_id = pysecrets.token_bytes(16)
    data_key = _derive_data_key(master, nonce, key_id)
    aead = AESGCM(data_key)

    dst.write(_MAGIC)
    dst.write(bytes([_VERSION]))
    dst.write(nonce)
    dst.write(key_id)

    counter = 0
    # First gzip-compress then encrypt — better compression ratio and
    # the AEAD tag still authenticates the compressed bytes.
    import io
    pipe = io.BytesIO()
    with gzip.GzipFile(fileobj=pipe, mode="wb", compresslevel=6) as gz:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            gz.write(chunk)
    pipe.seek(0)
    while True:
        plain = pipe.read(_CHUNK)
        if not plain:
            break
        # per-chunk nonce is base nonce XOR-counter for replay-safety
        ctr_bytes = counter.to_bytes(8, "big")
        per_nonce = bytes(a ^ b for a, b in zip(nonce, ctr_bytes + nonce[8:]))
        ct = aead.encrypt(per_nonce, plain, key_id)
        dst.write(struct.pack(">I", len(ct)))
        dst.write(ct)
        counter += 1

    return True, key_id.hex()


def decrypt_stream(src: IO[bytes], dst: IO[bytes]) -> bool:
    """Inverse of :func:`encrypt_stream`. Returns ``True`` if AEAD was used."""
    head = src.read(4)
    if head != _MAGIC:
        # not encrypted — assume it is a plain gzip
        src.seek(0)
        with gzip.GzipFile(fileobj=src, mode="rb") as gz:
            while True:
                chunk = gz.read(_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        return False

    if not _CRYPTO_OK:
        raise RuntimeError("dr archive is encrypted but cryptography lib missing")

    version = src.read(1)
    if not version or version[0] != _VERSION:
        raise RuntimeError(f"unsupported dr archive version: {version!r}")
    nonce = src.read(12)
    key_id = src.read(16)
    master = _load_or_create_master_key()
    data_key = _derive_data_key(master, nonce, key_id)
    aead = AESGCM(data_key)

    import io
    blob = io.BytesIO()
    counter = 0
    while True:
        len_bytes = src.read(4)
        if not len_bytes:
            break
        (length,) = struct.unpack(">I", len_bytes)
        ct = src.read(length)
        if len(ct) != length:
            raise RuntimeError("dr archive truncated")
        ctr_bytes = counter.to_bytes(8, "big")
        per_nonce = bytes(a ^ b for a, b in zip(nonce, ctr_bytes + nonce[8:]))
        plain = aead.decrypt(per_nonce, ct, key_id)
        blob.write(plain)
        counter += 1

    blob.seek(0)
    with gzip.GzipFile(fileobj=blob, mode="rb") as gz:
        while True:
            chunk = gz.read(_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
    return True
