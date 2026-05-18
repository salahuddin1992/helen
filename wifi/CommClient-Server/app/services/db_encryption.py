"""
At-rest encryption for the Helen SQLite database.

Two paths
---------
  1. **Native SQLCipher** — preferred. If ``pysqlcipher3`` is
     importable, every connection opens with PRAGMA key='…' and the
     entire DB file is AES-256 ciphertext. Drops in next to the
     existing sqlalchemy engine.

  2. **Application-layer envelope** — fallback for hosts where
     sqlcipher binaries can't be built (e.g. PyInstaller on Windows
     without VS build tools). Encrypts only sensitive *columns*
     (passwords, vault contents, e2ee key bundles) using AES-256-GCM
     with a key derived from a master passphrase.

Both paths derive their master key from ``HELEN_DB_MASTER_KEY`` env
var (or a file at ``$DATA_DIR/db-master.key``) via Argon2id, with
scrypt as fallback.

Usage
-----
    from app.services.db_encryption import (
        connect_encrypted, encrypt_field, decrypt_field,
    )

    # Native path:
    conn = connect_encrypted("/path/to/data.db", passphrase=os.environ["HELEN_DB_MASTER_KEY"])

    # Field-level fallback (always works):
    cipher = encrypt_field(b"secret payload", master_key)
    plain = decrypt_field(cipher, master_key)
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def derive_master_key(passphrase: str,
                      salt: bytes,
                      key_len: int = 32) -> bytes:
    """Argon2id (preferred) → scrypt (fallback). 32-byte output."""
    try:
        from argon2.low_level import hash_secret_raw, Type
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=3, memory_cost=64 * 1024,
            parallelism=2, hash_len=key_len, type=Type.ID,
        )
    except ImportError:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        kdf = Scrypt(salt=salt, length=key_len, n=2 ** 16, r=8, p=1)
        return kdf.derive(passphrase.encode("utf-8"))


def load_or_create_db_master_key(data_dir: str,
                                  passphrase: Optional[str] = None
                                  ) -> bytes:
    """Read ``$DATA_DIR/db-master.key`` (16-byte salt + the key).

    If absent and a ``passphrase`` is supplied, derive a fresh key
    via Argon2id and persist (mode 0600 / NTFS ACL on Windows).
    Without a passphrase, generate a 32-byte random key and store
    it directly. This file is what an operator must back up if they
    want to keep their DB readable.
    """
    p = Path(data_dir) / "db-master.key"
    if p.exists():
        return p.read_bytes()
    p.parent.mkdir(parents=True, exist_ok=True)

    if passphrase:
        salt = secrets.token_bytes(16)
        key = derive_master_key(passphrase, salt)
        # Format: 16-byte salt + 32-byte key. The salt lets us
        # recompute the key from the passphrase if the file is
        # ever lost.
        blob = salt + key
    else:
        blob = secrets.token_bytes(32)

    p.write_bytes(blob)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass

    if os.name == "nt":
        # Best-effort NTFS ACL lockdown — mirrors the .env hardening
        try:
            import subprocess
            subprocess.run(
                ["icacls", str(p),
                 "/inheritance:r",
                 "/grant:r", "SYSTEM:(R,W)",
                 "Administrators:(F)"],
                check=False, capture_output=True, timeout=8,
            )
        except Exception:
            pass

    return blob


def extract_key(blob: bytes) -> bytes:
    """Return the 32-byte AES key from a master-key blob.

    Handles both formats:
      * 32-byte raw key (random-generated)
      * 16-byte salt + 32-byte key (Argon2id-derived)
    """
    if len(blob) == 32:
        return blob
    if len(blob) >= 48:
        return blob[16:48]
    raise ValueError(f"db master key blob has unexpected length {len(blob)}")


# ── Native SQLCipher path ──────────────────────────────────────────


def connect_encrypted_sqlite(
    db_path: str, key: bytes,
):
    """Returns a sqlcipher3 connection with PRAGMA key set.

    Raises ImportError if pysqlcipher3 isn't available — callers
    should fall back to the field-level path in that case.
    """
    try:
        from pysqlcipher3 import dbapi2 as sqlcipher
    except ImportError as exc:
        raise ImportError(
            "pysqlcipher3 not installed; falling back to field-level "
            "encryption. Install from: "
            "https://github.com/rigglemania/pysqlcipher3"
        ) from exc

    conn = sqlcipher.connect(db_path)
    # PRAGMA key wants the key as a hex literal.
    hex_key = key.hex()
    conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    # Recommended pragmas for production
    conn.execute("PRAGMA cipher_page_size = 4096")
    conn.execute("PRAGMA kdf_iter = 256000")
    conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
    conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
    return conn


def is_native_encryption_available() -> bool:
    try:
        from pysqlcipher3 import dbapi2  # noqa: F401
        return True
    except ImportError:
        return False


# ── Field-level fallback (always works) ────────────────────────────


def encrypt_field(plaintext: bytes, key: bytes,
                   associated_data: bytes = b"") -> bytes:
    """Format::

        version(1) | nonce(12) | ciphertext+tag

    The version byte lets us rotate the cipher later without breaking
    existing data — older rows decrypt with the old algorithm,
    newly-encrypted rows use the new one.
    """
    nonce = secrets.token_bytes(12)
    aes = AESGCM(key)
    ct = aes.encrypt(nonce, plaintext, associated_data)
    return b"\x01" + nonce + ct


def decrypt_field(blob: bytes, key: bytes,
                   associated_data: bytes = b"") -> bytes:
    if len(blob) < 1 + 12 + 16:
        raise ValueError("encrypted blob too short")
    version = blob[0]
    if version != 1:
        raise ValueError(f"unsupported encrypted-field version {version}")
    nonce = blob[1:13]
    ct = blob[13:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, associated_data)


# ── High-level helpers for SQLAlchemy column types ─────────────────


@dataclass
class EncryptedString:
    """Wrapper that lets a SQLAlchemy column transparently
    encrypt/decrypt str values. Use as a TypeDecorator subclass:

        class HelenEncryptedStr(types.TypeDecorator):
            impl = types.LargeBinary
            cache_ok = True
            def __init__(self, *args, key_fn, **kw):
                super().__init__(*args, **kw)
                self.key_fn = key_fn
            def process_bind_param(self, value, dialect):
                if value is None: return None
                return EncryptedString.encrypt(value, self.key_fn())
            def process_result_value(self, value, dialect):
                if value is None: return None
                return EncryptedString.decrypt(value, self.key_fn())
    """

    @staticmethod
    def encrypt(value: str, key: bytes) -> bytes:
        return encrypt_field(value.encode("utf-8"), key)

    @staticmethod
    def decrypt(blob: bytes, key: bytes) -> str:
        return decrypt_field(blob, key).decode("utf-8")
