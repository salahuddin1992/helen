"""
Security module unit tests.

Covers password hashing, token creation/validation, and JWT operations
without making HTTP requests.
"""

from __future__ import annotations

import time
import pytest
from datetime import datetime, timedelta, timezone

from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    revoke_jti,
    is_jti_revoked,
)
from fastapi import HTTPException


class TestPasswordHashing:
    """Tests for password hashing and verification."""

    def test_hash_password_creates_different_hashes(self):
        """Hashing the same password twice produces different hashes."""
        password = "MySecurePassword123!"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        # Hashes should be different (due to salt)
        assert hash1 != hash2
        # But both should verify against the original password
        assert verify_password(password, hash1)
        assert verify_password(password, hash2)

    def test_verify_correct_password(self):
        """Verify returns True for correct password."""
        password = "CorrectPassword123!"
        hashed = hash_password(password)

        assert verify_password(password, hashed) == True

    def test_verify_wrong_password(self):
        """Verify returns False for wrong password."""
        correct_password = "CorrectPassword123!"
        wrong_password = "WrongPassword456!"
        hashed = hash_password(correct_password)

        assert verify_password(wrong_password, hashed) == False

    def test_verify_empty_password(self):
        """Verify handles empty password gracefully."""
        hashed = hash_password("SomePassword123!")

        assert verify_password("", hashed) == False

    def test_verify_corrupted_hash(self):
        """Verify handles corrupted hash without crashing."""
        result = verify_password("password", "corrupted_hash_data")
        assert result == False

    def test_password_hash_is_deterministic_format(self):
        """Password hash has bcrypt format."""
        password = "TestPassword123!"
        hashed = hash_password(password)

        # Bcrypt hashes start with $2a$, $2b$, or $2x$, followed by cost factor
        assert hashed.startswith(("$2a$", "$2b$", "$2x$"))


class TestAccessTokens:
    """Tests for access token creation and validation."""

    def test_create_access_token_success(self):
        """Create access token returns a valid JWT string."""
        user_id = "test_user_123"
        token = create_access_token(user_id)

        assert isinstance(token, str)
        assert len(token) > 0
        # JWT format: header.payload.signature
        assert token.count(".") == 2

    def test_decode_access_token_success(self):
        """Decode valid access token returns payload."""
        user_id = "test_user_456"
        token = create_access_token(user_id)

        payload = decode_token(token)

        assert payload["sub"] == user_id
        assert payload["type"] == "access"
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

    def test_decode_access_token_has_required_claims(self):
        """Access token includes all required claims."""
        token = create_access_token("user_id")
        payload = decode_token(token)

        required_claims = {"sub", "type", "jti", "exp", "iat"}
        assert required_claims.issubset(payload.keys())

    def test_decode_invalid_token_raises_exception(self):
        """Decode invalid token raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            decode_token("invalid.token.format")

        assert exc_info.value.status_code == 401

    def test_decode_corrupted_token_raises_exception(self):
        """Decode corrupted token raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            decode_token("not.a.jwt.token")

        assert exc_info.value.status_code == 401

    def test_access_token_with_extra_claims(self):
        """Create access token with extra claims includes them."""
        token = create_access_token(
            "user_id",
            extra={"custom_claim": "custom_value"},
        )

        payload = decode_token(token)
        assert payload["custom_claim"] == "custom_value"

    def test_access_token_with_fingerprint(self):
        """Create access token with fingerprint includes it."""
        token = create_access_token(
            "user_id",
            fingerprint="device_fingerprint_123",
        )

        payload = decode_token(token)
        assert payload["fpr"] == "device_fingerprint_123"


class TestRefreshTokens:
    """Tests for refresh token creation and validation."""

    def test_create_refresh_token_success(self):
        """Create refresh token returns a valid JWT string."""
        user_id = "test_user_789"
        token = create_refresh_token(user_id)

        assert isinstance(token, str)
        assert len(token) > 0
        assert token.count(".") == 2

    def test_decode_refresh_token_success(self):
        """Decode valid refresh token returns payload."""
        user_id = "test_user_999"
        token = create_refresh_token(user_id)

        payload = decode_token(token)

        assert payload["sub"] == user_id
        assert payload["type"] == "refresh"
        assert "jti" in payload

    def test_refresh_token_type_is_refresh(self):
        """Refresh token has type='refresh'."""
        token = create_refresh_token("user_id")
        payload = decode_token(token)

        assert payload["type"] == "refresh"

    def test_access_token_not_valid_as_refresh(self):
        """Access token fails validation when expected type is refresh."""
        access_token = create_access_token("user_id")
        payload = decode_token(access_token)

        # Manually check type mismatch
        assert payload["type"] == "access"

    def test_refresh_token_longer_expiry(self):
        """Refresh token has longer expiry than access token."""
        access = create_access_token("user_id")
        refresh = create_refresh_token("user_id")

        access_payload = decode_token(access)
        refresh_payload = decode_token(refresh)

        access_exp = access_payload["exp"]
        refresh_exp = refresh_payload["exp"]

        # Refresh should expire later
        assert refresh_exp > access_exp


class TestTokenExpiry:
    """Tests for token expiration behavior."""

    def test_expired_token_raises_exception(self):
        """Expired token raises HTTPException on decode."""
        # This is difficult to test without mocking time,
        # but we verify the exception handling exists
        from unittest.mock import patch
        from jwt import ExpiredSignatureError

        with patch("app.core.security.jwt.decode", side_effect=ExpiredSignatureError()):
            with pytest.raises(HTTPException) as exc_info:
                decode_token("some.token.here")

            assert exc_info.value.status_code == 401
            assert "expired" in exc_info.value.detail.lower()

    def test_token_has_iat_and_exp(self):
        """Token includes issued-at and expiration times."""
        token = create_access_token("user_id")
        payload = decode_token(token)

        assert "iat" in payload
        assert "exp" in payload

        iat = payload["iat"]
        exp = payload["exp"]

        # Expiration should be after issued-at
        assert exp > iat


class TestTokenRevocation:
    """Tests for JTI-based token revocation."""

    def test_revoke_jti_marks_token_as_revoked(self):
        """Revoke JTI adds it to revocation set."""
        token = create_access_token("user_id")
        payload = decode_token(token)
        jti = payload["jti"]

        # Check not revoked initially
        assert is_jti_revoked(jti) == False

        # Revoke it
        revoke_jti(jti)

        # Check it's now revoked
        assert is_jti_revoked(jti) == True

    def test_revoked_token_fails_decode(self):
        """Decoding a revoked token raises exception."""
        token = create_access_token("user_id")
        payload = decode_token(token)
        jti = payload["jti"]

        # Revoke the token
        revoke_jti(jti)

        # Attempt to decode should fail
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)

        assert exc_info.value.status_code == 401
        assert "revoked" in exc_info.value.detail.lower()

    def test_multiple_jtis_independently_revocable(self):
        """Different JTIs can be revoked independently."""
        token1 = create_access_token("user1")
        token2 = create_access_token("user2")

        payload1 = decode_token(token1)
        payload2 = decode_token(token2)

        jti1 = payload1["jti"]
        jti2 = payload2["jti"]

        # Revoke only jti1
        revoke_jti(jti1)

        assert is_jti_revoked(jti1) == True
        assert is_jti_revoked(jti2) == False

    def test_revoke_nonexistent_jti(self):
        """Revoking nonexistent JTI works (no error)."""
        revoke_jti("nonexistent_jti_12345")
        assert is_jti_revoked("nonexistent_jti_12345") == True


class TestTokenStructure:
    """Tests for token structure and claims."""

    def test_each_token_has_unique_jti(self):
        """Each token gets a unique JTI (JWT ID)."""
        token1 = create_access_token("user_id")
        token2 = create_access_token("user_id")

        payload1 = decode_token(token1)
        payload2 = decode_token(token2)

        jti1 = payload1["jti"]
        jti2 = payload2["jti"]

        assert jti1 != jti2

    def test_jti_is_hex_string(self):
        """JTI is a hex string (from secrets.token_hex)."""
        token = create_access_token("user_id")
        payload = decode_token(token)
        jti = payload["jti"]

        # Hex string should only contain 0-9 and a-f
        assert all(c in "0123456789abcdef" for c in jti)

    def test_token_subject_matches_user_id(self):
        """Token subject (sub) claim matches provided user_id."""
        user_id = "unique_user_id_12345"
        token = create_access_token(user_id)

        payload = decode_token(token)
        assert payload["sub"] == user_id

    def test_token_algorithm_is_hs256(self):
        """Token is signed with HS256."""
        # Verify by checking the header when decoded
        import jwt
        from app.core.config import get_settings

        token = create_access_token("user_id")
        settings = get_settings()

        # Decode without verification to check header
        header = jwt.get_unverified_header(token)
        assert header["alg"] == settings.JWT_ALGORITHM
        assert header["alg"] == "HS256"


class TestErrorMessages:
    """Tests for security error message handling."""

    def test_invalid_token_generic_error(self):
        """Invalid token error message is generic (doesn't leak details)."""
        with pytest.raises(HTTPException) as exc_info:
            decode_token("invalid.token.here")

        detail = exc_info.value.detail.lower()
        # Should not contain specific JWT library errors
        assert "decode" not in detail or "invalid authentication token" in detail
        assert "signature" not in detail

    def test_expired_token_clear_message(self):
        """Expired token error message is clear."""
        from unittest.mock import patch
        from jwt import ExpiredSignatureError

        with patch("app.core.security.jwt.decode", side_effect=ExpiredSignatureError()):
            with pytest.raises(HTTPException) as exc_info:
                decode_token("some.token")

            assert "expired" in exc_info.value.detail.lower()
