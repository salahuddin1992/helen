"""
End-to-End Encryption schemas (Pydantic v2).

X3DH key bundle format follows Signal Protocol conventions.
All key material is base64-encoded on the wire.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KeyBundleUpload(BaseModel):
    """
    User uploads their key material for the first time or refreshes.
    Identity key is permanent; signed pre-key rotates; one-time pre-keys are consumed.
    """

    identity_key: str = Field(..., description="Base64-encoded identity public key")
    signed_pre_key: str = Field(..., description="Base64-encoded signed pre-key public key")
    signed_pre_key_signature: str = Field(
        ..., description="Base64-encoded signature of spk by identity key"
    )
    one_time_pre_keys: list[str] = Field(
        default_factory=list,
        description="List of base64-encoded one-time pre-key public keys (batch upload, max 100)",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "identity_key": "base64encodedPublicKey...",
                "signed_pre_key": "base64encodedSignedPreKey...",
                "signed_pre_key_signature": "base64encodedSignature...",
                "one_time_pre_keys": [
                    "base64key1...",
                    "base64key2...",
                    "base64key3...",
                ],
            }
        }


class KeyBundleResponse(BaseModel):
    """
    Server returns a key bundle for the target user (used in X3DH key agreement).
    If one-time pre-keys are exhausted, optional field is null.
    """

    identity_key: str = Field(..., description="Target user's identity key (base64)")
    signed_pre_key: str = Field(..., description="Target user's current signed pre-key (base64)")
    signed_pre_key_id: int = Field(..., description="Signed pre-key version ID")
    signed_pre_key_signature: str = Field(
        ..., description="Signature of spk by identity key (base64)"
    )
    one_time_pre_key: str | None = Field(
        None, description="One-time pre-key (base64) if available, else null"
    )
    one_time_pre_key_id: int | None = Field(
        None, description="One-time pre-key ID if available, else null"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "identity_key": "base64...",
                "signed_pre_key": "base64...",
                "signed_pre_key_id": 5,
                "signed_pre_key_signature": "base64...",
                "one_time_pre_key": "base64...",
                "one_time_pre_key_id": 42,
            }
        }


class SessionEstablished(BaseModel):
    """Confirmation that an encrypted session has been established."""

    session_id: str = Field(
        ..., description="Unique session ID (typically hash of initial key agreement)"
    )
    initiator_id: str = Field(..., description="User ID of the initiator")
    responder_id: str = Field(..., description="User ID of the responder")

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "abc123def456...",
                "initiator_id": "user1uuid",
                "responder_id": "user2uuid",
            }
        }


class PreKeyCountResponse(BaseModel):
    """Response with remaining one-time pre-key count."""

    remaining_pre_keys: int = Field(..., description="Number of unused one-time pre-keys")
    should_rotate: bool = Field(
        ..., description="True if count < 10, user should upload more keys"
    )


class SignedPreKeyRotateRequest(BaseModel):
    """Request to rotate the signed pre-key."""

    signed_pre_key: str = Field(..., description="New signed pre-key (base64)")
    signed_pre_key_signature: str = Field(
        ..., description="Signature by identity key (base64)"
    )


class SignedPreKeyRotateResponse(BaseModel):
    """Confirmation of signed pre-key rotation."""

    key_id: int = Field(..., description="New key version ID")
    activated_at: str = Field(..., description="ISO 8601 timestamp when rotation took effect")
