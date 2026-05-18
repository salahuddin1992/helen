"""
TOTPManager — RFC-6238 TOTP (HMAC-SHA1, 6 digits, 30s window).

Prefers ``pyotp`` when available; falls back to an inline implementation
so the onboarding flow remains functional in restricted environments.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote

try:
    import pyotp  # type: ignore
    _HAS_PYOTP = True
except Exception:
    _HAS_PYOTP = False


class TOTPManager:
    """RFC-6238 compatible TOTP generator/validator."""

    DEFAULT_DIGITS = 6
    DEFAULT_INTERVAL = 30

    def generate_secret(self, length_bytes: int = 20) -> str:
        """Return a base32-encoded random secret (160 bits by default)."""
        if _HAS_PYOTP:
            return pyotp.random_base32(length=length_bytes if length_bytes > 16 else 32)
        raw = os.urandom(length_bytes)
        return base64.b32encode(raw).decode("ascii").rstrip("=")

    def now(self, secret: str) -> str:
        if _HAS_PYOTP:
            return pyotp.TOTP(secret).now()
        return self._compute(secret, int(time.time()) // self.DEFAULT_INTERVAL)

    def verify(self, secret: str, code: str, window: int = 1) -> bool:
        """Verify ``code`` against ``secret`` allowing ±``window`` intervals."""
        if not secret or not code:
            return False
        code = code.strip()
        if _HAS_PYOTP:
            return pyotp.TOTP(secret).verify(code, valid_window=window)
        counter = int(time.time()) // self.DEFAULT_INTERVAL
        for offset in range(-window, window + 1):
            if hmac.compare_digest(self._compute(secret, counter + offset), code):
                return True
        return False

    def provisioning_uri(self, account: str, issuer: str, secret: str) -> str:
        """Construct an otpauth:// URI suitable for QR encoding."""
        if _HAS_PYOTP:
            return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)
        label = quote(f"{issuer}:{account}", safe="")
        params = f"secret={secret}&issuer={quote(issuer)}&digits={self.DEFAULT_DIGITS}&period={self.DEFAULT_INTERVAL}"
        return f"otpauth://totp/{label}?{params}"

    # ── inline RFC-6238 ─────────────────────────────────
    def _compute(self, secret: str, counter: int) -> str:
        # Pad base32 to multiple of 8 for decoders that need it.
        pad = "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode((secret + pad).upper())
        msg = struct.pack(">Q", counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = ((digest[offset] & 0x7F) << 24) | \
                 ((digest[offset + 1] & 0xFF) << 16) | \
                 ((digest[offset + 2] & 0xFF) << 8) | \
                 (digest[offset + 3] & 0xFF)
        code = binary % (10 ** self.DEFAULT_DIGITS)
        return str(code).zfill(self.DEFAULT_DIGITS)
