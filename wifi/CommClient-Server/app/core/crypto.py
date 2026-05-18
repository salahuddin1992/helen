"""
Cryptographic utilities for CommClient server.

Provides:
  - Secure token generation
  - HMAC signing/verification
  - AES-GCM field-level encryption for sensitive data at rest
  - Constant-time string comparison
  - Token fingerprinting (binds token to client identity)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


# ── Secure Token Generation ──────────────────────────────

def generate_secure_token(nbytes: int = 32) -> str:
    """Generate a cryptographically secure URL-safe token."""
    return secrets.token_urlsafe(nbytes)


def generate_nonce(nbytes: int = 16) -> str:
    """Generate a nonce for replay protection."""
    return secrets.token_hex(nbytes)


# ── HMAC Signing ─────────────────────────────────────────

def hmac_sign(data: str, key: Optional[str] = None) -> str:
    """
    Sign a string with HMAC-SHA256.
    Uses JWT_SECRET as default key.
    """
    if key is None:
        key = get_settings().JWT_SECRET
    return hmac.new(
        key.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hmac_verify(data: str, signature: str, key: Optional[str] = None) -> bool:
    """
    Verify an HMAC-SHA256 signature using constant-time comparison.
    """
    expected = hmac_sign(data, key)
    return hmac.compare_digest(expected, signature)


# ── Constant-Time Comparison ─────────────────────────────

def safe_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── Token Fingerprinting ────────────────────────────────

def compute_token_fingerprint(
    user_id: str,
    ip_address: str,
    user_agent: str = "",
) -> str:
    """
    Compute a fingerprint for token binding.
    Binds a JWT to the client identity (IP + UA).
    Used for optional token replay detection.
    """
    raw = f"{user_id}:{ip_address}:{user_agent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Refresh Token Hashing ────────────────────────────────
# Using SHA-256 with HMAC-keyed prefix to avoid rainbow tables

def hash_refresh_token(token: str) -> str:
    """
    Hash a refresh token for storage.
    Uses HMAC-SHA256 with the JWT secret as key — prevents rainbow table attacks
    on the token_hash column even if the DB is leaked.
    """
    key = get_settings().JWT_SECRET
    return hmac.new(
        key.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ── AES-GCM Field Encryption ────────────────────────────

def _derive_field_key() -> bytes:
    """
    Derive a 256-bit AES key from the JWT secret.
    This is a simple derivation for field-level encryption at rest.
    For production with key rotation, use a proper KMS.
    """
    secret = get_settings().JWT_SECRET
    return hashlib.sha256(f"field-enc:{secret}".encode("utf-8")).digest()


def encrypt_field(plaintext: str) -> str:
    """
    Encrypt a string field with AES-256-GCM.
    Returns base64-encoded (nonce + ciphertext + tag).
    """
    key = _derive_field_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return urlsafe_b64encode(nonce + ct).decode("utf-8")


def decrypt_field(ciphertext_b64: str) -> str:
    """
    Decrypt a field encrypted with encrypt_field().
    """
    key = _derive_field_key()
    aesgcm = AESGCM(key)
    raw = urlsafe_b64decode(ciphertext_b64)
    nonce = raw[:12]
    ct = raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


# ── Password Strength Validation ─────────────────────────

def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    Validate password meets minimum strength requirements.
    Returns (is_valid, error_message).
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password must be at most 128 characters"
    if password.isdigit():
        return False, "Password cannot be all digits"
    if password.isalpha():
        return False, "Password must contain at least one digit or special character"
    return True, ""
