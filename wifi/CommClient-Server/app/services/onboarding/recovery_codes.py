"""
Admin recovery codes — cryptographically random 8-char codes.

Codes are shown to the operator once at generation time; only their
hashes are persisted. Verification uses constant-time comparison.

Format: ``XXXX-XXXX`` (8 chars + separator), Crockford base32 alphabet
(no I, L, O, U) for ergonomic transcription.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def generate_recovery_codes(count: int = 10) -> list[str]:
    """Generate ``count`` unique 8-char Crockford base32 codes."""
    out: set[str] = set()
    while len(out) < count:
        out.add(_one())
    return [f"{c[:4]}-{c[4:]}" for c in sorted(out)]


def _one() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


def normalize(code: str) -> str:
    """Strip separators and upper-case for hashing."""
    return code.replace("-", "").replace(" ", "").upper()


def hash_recovery_code(code: str) -> str:
    """SHA-256 over normalized code with a domain separator."""
    norm = normalize(code).encode("ascii")
    return hashlib.sha256(b"helen-recovery-v1|" + norm).hexdigest()


def verify_recovery_code(code: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), stored_hash)
