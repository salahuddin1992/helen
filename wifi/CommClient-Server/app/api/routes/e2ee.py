"""
End-to-End Encryption REST endpoints — key bundle upload/fetch, session registration.

Security model:
  - All endpoints require authentication (bearer token)
  - Key bundle fetch is available to any authenticated user (needed for X3DH setup)
  - Users can only manage their own key material
  - One-time key consumption is atomic and idempotent
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.schemas.e2ee import (
    KeyBundleResponse,
    KeyBundleUpload,
    PreKeyCountResponse,
    SessionEstablished,
    SignedPreKeyRotateRequest,
    SignedPreKeyRotateResponse,
)
from app.services.e2ee_service import E2EEService

logger = get_logger(__name__)

router = APIRouter(prefix="/e2ee", tags=["e2ee"])


@router.post("/keys", response_model=dict)
async def upload_key_bundle(
    bundle: KeyBundleUpload,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload or update user's E2EE key bundle.

    Request must include:
      - identity_key: Base64-encoded identity public key (immutable after first upload)
      - signed_pre_key: Base64-encoded signed pre-key
      - signed_pre_key_signature: Base64-encoded signature by identity key
      - one_time_pre_keys: List of base64-encoded one-time pre-keys (optional, max 100)

    Response:
      - identity_key_version: Version of stored identity key
      - signed_pre_key_id: Version of new signed pre-key
      - one_time_pre_keys_stored: Count of newly stored OTP keys

    Security:
      - Identity key is permanent and cannot change
      - Signed pre-key rotation deactivates previous versions
      - Old OTP keys remain valid until consumed
      - Server never accesses plaintext
    """
    try:
        result = await E2EEService.upload_key_bundle(
            db=db,
            user_id=user_id,
            identity_key=bundle.identity_key,
            signed_pre_key=bundle.signed_pre_key,
            signed_pre_key_signature=bundle.signed_pre_key_signature,
            one_time_pre_keys=bundle.one_time_pre_keys,
        )
        logger.info("e2ee_bundle_upload_success", user_id=user_id)
        return result
    except Exception as e:
        logger.error("e2ee_bundle_upload_error", user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/keys/{target_user_id}", response_model=KeyBundleResponse)
async def get_key_bundle(
    target_user_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch key bundle for X3DH key agreement.

    Returns the target user's:
      - Identity key
      - Active signed pre-key (with ID and signature)
      - One unused one-time pre-key (if available)

    Security:
      - One-time pre-key consumption is atomic (no race conditions)
      - Consumed OTP key is marked as used and attributed to fetching user
      - Pre-key consumption is logged for audit
      - Returns null for one_time_pre_key if supply exhausted

    Use case:
      - Client A calls this to fetch Client B's bundle
      - Client A performs X3DH and sends first encrypted message
      - Client B receives encrypted message and retrieves Client A's bundle
      - Both clients establish Double Ratchet state
    """
    try:
        bundle = await E2EEService.get_key_bundle(
            db=db,
            target_user_id=target_user_id,
            consumer_user_id=user_id,
        )
        logger.info(
            "e2ee_bundle_fetch_success",
            fetcher=user_id,
            target=target_user_id,
            otpk_available=bundle["one_time_pre_key"] is not None,
        )
        return KeyBundleResponse(**bundle)
    except Exception as e:
        logger.error(
            "e2ee_bundle_fetch_error",
            fetcher=user_id,
            target=target_user_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.get("/keys/me/count", response_model=PreKeyCountResponse)
async def get_pre_key_count(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Check remaining one-time pre-key count.

    Returns:
      - remaining_pre_keys: Count of unused OTP keys
      - should_rotate: True if count < 10 (client should upload more keys)

    Client should proactively upload new one-time pre-keys when count drops below threshold
    to avoid session establishment failures due to key exhaustion.
    """
    count = await E2EEService.get_pre_key_count(db, user_id)
    return PreKeyCountResponse(
        remaining_pre_keys=count,
        should_rotate=count < 10,
    )


@router.post("/keys/rotate", response_model=SignedPreKeyRotateResponse)
async def rotate_signed_pre_key(
    request: SignedPreKeyRotateRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Rotate signed pre-key (without uploading one-time pre-keys).

    Call this periodically (e.g., every 30 days) to maintain forward secrecy.

    Request:
      - signed_pre_key: Base64-encoded new signed pre-key
      - signed_pre_key_signature: Base64-encoded signature by identity key

    Response:
      - key_id: New version ID
      - activated_at: ISO 8601 timestamp when rotation took effect

    Security:
      - Old signed pre-keys are deactivated but retained briefly for retransmission
      - Client should send the rotated key to all active peers
      - Rotation does not affect existing sessions (new messages use new key)
    """
    try:
        result = await E2EEService.rotate_signed_pre_key(
            db=db,
            user_id=user_id,
            new_signed_pre_key=request.signed_pre_key,
            signature=request.signed_pre_key_signature,
        )
        logger.info("e2ee_spk_rotation_success", user_id=user_id)
        return SignedPreKeyRotateResponse(**result)
    except Exception as e:
        logger.error("e2ee_spk_rotation_error", user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/sessions", response_model=SessionEstablished)
async def register_e2ee_session(
    session_data: SessionEstablished,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Register an encrypted session after X3DH key agreement completion.

    Called by either party (initiator or responder) after successful X3DH.
    Idempotent: duplicate registrations return existing session.

    Request:
      - session_id: Unique session ID (typically hash of initial DH outputs)
      - initiator_id: User who fetched responder's bundle and started X3DH
      - responder_id: User whose bundle was fetched

    Response: Confirmed session metadata

    Security:
      - Session metadata is stored for audit and re-keying purposes
      - Double Ratchet state is maintained client-side (not on server)
      - Server tracks active sessions to enable rollover/re-keying
      - User making the call must match initiator or responder ID
    """
    # Validate that caller is a party to this session
    if user_id not in (session_data.initiator_id, session_data.responder_id):
        logger.warning(
            "e2ee_session_unauthorized_register",
            user_id=user_id,
            initiator=session_data.initiator_id,
            responder=session_data.responder_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a party (initiator or responder) to register a session",
        )

    try:
        session = await E2EEService.register_session(
            db=db,
            initiator_id=session_data.initiator_id,
            responder_id=session_data.responder_id,
            session_id=session_data.session_id,
        )
        return SessionEstablished(
            session_id=session.session_id,
            initiator_id=session.initiator_id,
            responder_id=session.responder_id,
        )
    except Exception as e:
        logger.error(
            "e2ee_session_register_error",
            user_id=user_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register session",
        )
