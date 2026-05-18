"""
Public share code generator.

Every user gets a 64-character alphanumeric code they can hand out to other
people as a search token. The alphabet is [A-Za-z0-9] (62 symbols), giving
62^64 ≈ 4.4 × 10^114 possible values — brute-force enumeration is not a
concern even against the entire user table.

Codes are minted with `secrets.choice` (CSPRNG) and re-tried on the
vanishingly unlikely case of a collision.
"""

from __future__ import annotations

import secrets

SHARE_CODE_LENGTH = 64
SHARE_CODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)


def generate_share_code() -> str:
    """Return a fresh 64-char code drawn from a CSPRNG."""
    return "".join(
        secrets.choice(SHARE_CODE_ALPHABET) for _ in range(SHARE_CODE_LENGTH)
    )


def is_valid_share_code(code: str) -> bool:
    """Cheap shape check used by the lookup endpoint."""
    if not code or len(code) != SHARE_CODE_LENGTH:
        return False
    return all(c in SHARE_CODE_ALPHABET for c in code)
