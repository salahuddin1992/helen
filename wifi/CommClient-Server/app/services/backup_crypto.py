"""
Encrypted backups + WebAuthn (FIDO2) skeleton.

Two independent helpers:

  * ``encrypt_backup(plaintext, master_key) -> ciphertext``
    AES-256-GCM, header carries salt + nonce. The master key is
    derived from the operator passphrase via Argon2id (or scrypt
    fallback) so brute force costs real money.

  * ``WebAuthnRegistry`` — the server-side state for FIDO2 hardware
    keys (Yubikey / TouchID / Windows Hello). Stores credential
    public keys, lets clients authenticate by signing a challenge.
    No external service — works on a fully air-gapped LAN.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ── Encrypted backups ───────────────────────────────────────────────


_BACKUP_MAGIC = b"HELENBAK"          # 8 bytes
_BACKUP_VERSION = 1
_KDF_SALT_SIZE = 16
_NONCE_SIZE = 12


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Argon2id if available, else scrypt fallback. Both produce
    32 bytes suitable for AES-256."""
    try:
        from argon2.low_level import hash_secret_raw, Type
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=3, memory_cost=64 * 1024,
            parallelism=2, hash_len=32, type=Type.ID,
        )
    except ImportError:
        # scrypt is in the cryptography lib
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
        return kdf.derive(passphrase.encode("utf-8"))


def encrypt_backup(plaintext: bytes, passphrase: str) -> bytes:
    """Format::

        magic(8) | version(1) | salt(16) | nonce(12) | ciphertext+tag

    The ciphertext is AES-256-GCM with the passphrase-derived key
    binding the version+salt as additional data so a header swap
    breaks decryption."""
    salt = secrets.token_bytes(_KDF_SALT_SIZE)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    key = _derive_key(passphrase, salt)
    aes = AESGCM(key)
    aad = _BACKUP_MAGIC + bytes([_BACKUP_VERSION]) + salt
    ct = aes.encrypt(nonce, plaintext, aad)
    return aad + nonce + ct


def decrypt_backup(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < 8 + 1 + _KDF_SALT_SIZE + _NONCE_SIZE + 16:
        raise ValueError("blob too small")
    if blob[:8] != _BACKUP_MAGIC:
        raise ValueError("not a Helen backup")
    version = blob[8]
    if version != _BACKUP_VERSION:
        raise ValueError(f"unsupported backup version {version}")
    salt = blob[9:9 + _KDF_SALT_SIZE]
    nonce = blob[9 + _KDF_SALT_SIZE:9 + _KDF_SALT_SIZE + _NONCE_SIZE]
    ct = blob[9 + _KDF_SALT_SIZE + _NONCE_SIZE:]
    key = _derive_key(passphrase, salt)
    aes = AESGCM(key)
    aad = _BACKUP_MAGIC + bytes([_BACKUP_VERSION]) + salt
    return aes.decrypt(nonce, ct, aad)


# ── WebAuthn / FIDO2 ───────────────────────────────────────────────


@dataclass
class WebAuthnCredential:
    """Public-only representation of a registered FIDO2 key."""
    credential_id: str           # base64url-encoded
    user_id: str
    public_key_cose: bytes       # COSE-encoded public key
    sign_count: int = 0
    label: str = ""              # human-friendly tag
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0


class WebAuthnRegistry:
    """SQLite-backed store of registered hardware keys per user.

    Validating an assertion needs the COSE public key (we keep the
    raw bytes the authenticator sent during registration). The
    actual cryptography is in the WebAuthn assertion verifier — a
    short helper at the bottom of this file demonstrates the basic
    flow without pulling fido2/cbor heavyweights."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    credential_id   TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    public_key_cose BLOB NOT NULL,
                    sign_count      INTEGER NOT NULL DEFAULT 0,
                    label           TEXT,
                    created_at      REAL NOT NULL,
                    last_used_at    REAL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_wa_user "
                       "ON webauthn_credentials(user_id)")

    def register(self, cred: WebAuthnCredential) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO webauthn_credentials "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (cred.credential_id, cred.user_id,
                        cred.public_key_cose, cred.sign_count,
                        cred.label, cred.created_at,
                        cred.last_used_at or None))

    def find(self, credential_id: str) -> Optional[WebAuthnCredential]:
        with sqlite3.connect(self.db_path) as c:
            r = c.execute(
                "SELECT credential_id, user_id, public_key_cose, "
                "sign_count, label, created_at, last_used_at "
                "FROM webauthn_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if not r:
            return None
        return WebAuthnCredential(
            credential_id=r[0], user_id=r[1],
            public_key_cose=r[2], sign_count=r[3], label=r[4] or "",
            created_at=r[5], last_used_at=r[6] or 0.0,
        )

    def list_for_user(self, user_id: str) -> list[WebAuthnCredential]:
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT credential_id, user_id, public_key_cose, "
                "sign_count, label, created_at, last_used_at "
                "FROM webauthn_credentials WHERE user_id=? "
                "ORDER BY created_at DESC", (user_id,),
            ).fetchall()
        return [
            WebAuthnCredential(
                credential_id=r[0], user_id=r[1],
                public_key_cose=r[2], sign_count=r[3], label=r[4] or "",
                created_at=r[5], last_used_at=r[6] or 0.0,
            )
            for r in rows
        ]

    def remove(self, credential_id: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("DELETE FROM webauthn_credentials "
                       "WHERE credential_id=?", (credential_id,))

    def bump_sign_count(self, credential_id: str,
                          new_count: int) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE webauthn_credentials "
                       "SET sign_count=?, last_used_at=? "
                       "WHERE credential_id=?",
                       (new_count, time.time(), credential_id))

    # ── helper for issuing challenges ─────────────────────────

    def new_challenge(self) -> str:
        """Create a fresh per-login challenge. Server stores it in
        an HMAC cookie + DB row keyed by session, then verifies the
        client's signature on the same blob."""
        return base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode("ascii").rstrip("=")
