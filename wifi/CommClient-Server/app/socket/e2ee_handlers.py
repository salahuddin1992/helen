"""
End-to-End Encryption socket events — key bundle updates, session establishment, pre-key notifications.

Real-time notification architecture:
  - Users are notified when peers update their key bundles (requires re-keying conversation)
  - Session establishment requests can be forwarded through server (e.g., for offline delivery)
  - Pre-key exhaustion alerts prompt proactive key uploads before failures occur
  - All events require authentication (per socket.io auth in server.py)
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.core.security_utils import is_valid_uuid
from app.db.session import async_session_factory
from app.services.e2ee_service import E2EEService
from app.services.presence_service import presence_service
from app.socket.server import emit_to_user, get_user_id, sio

logger = get_logger(__name__)


@sio.event
async def e2ee_key_bundle_updated(sid: str, data: dict[str, Any]):
    """
    User broadcasts that they have updated their key bundle.

    Notifies all peers that they should refetch the key bundle before starting
    new sessions (e.g., if signed pre-key was rotated).

    Client sends:
      {
        "new_spk_id": int,  # Version of newly rotated SPK
      }

    Security:
      - User ID extracted from socket session (cannot be spoofed)
      - Message is broadcast to all peers (via presence tracking)
      - Peers can choose to ignore if using existing session
    """
    user_id = await get_user_id(sid)
    if not user_id:
        logger.warning("e2ee_key_updated_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict) or "new_spk_id" not in data:
            logger.warning("e2ee_key_updated_invalid_data", user_id=user_id, data=data)
            return

        new_spk_id = data.get("new_spk_id")
        if not isinstance(new_spk_id, int) or new_spk_id < 1:
            logger.warning("e2ee_key_updated_invalid_spk_id", user_id=user_id, spk_id=new_spk_id)
            return

        # Broadcast to all other connected clients (not just direct peers)
        # Peers will fetch new bundle on next session initiation
        await sio.emit(
            "e2ee:key_bundle_updated",
            {
                "user_id": user_id,
                "new_spk_id": new_spk_id,
            },
            skip_sid=sid,
        )

        logger.info(
            "e2ee_key_updated_broadcast",
            user_id=user_id,
            new_spk_id=new_spk_id,
        )
    except Exception as e:
        logger.error("e2ee_key_updated_error", user_id=user_id, error=str(e))


@sio.event
async def e2ee_session_request(sid: str, data: dict[str, Any]):
    """
    User initiates encrypted session with a peer (X3DH key agreement).

    This event allows sending session establishment data through the server
    (useful if responder is offline; they will receive on reconnect).

    Client sends:
      {
        "responder_id": str,  # Target user ID
        "key_agreement_data": str,  # X3DH initial message (JSON or base64)
      }

    Server forwards to responder (if online) or stores for offline delivery.

    Security:
      - Initiator ID is extracted from socket session (cannot be spoofed)
      - Message is encrypted end-to-end (server never sees plaintext)
      - Responder must accept the session explicitly
      - Prevents unsolicited session creation (optional: require prior contact)
    """
    initiator_id = await get_user_id(sid)
    if not initiator_id:
        logger.warning("e2ee_session_request_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict):
            logger.warning("e2ee_session_request_invalid", initiator_id=initiator_id)
            return

        responder_id = data.get("responder_id")
        key_agreement_data = data.get("key_agreement_data")

        if not responder_id or not key_agreement_data:
            logger.warning(
                "e2ee_session_request_missing_fields",
                initiator_id=initiator_id,
                responder_id=responder_id,
            )
            return

        if not is_valid_uuid(responder_id):
            logger.warning(
                "e2ee_session_request_invalid_responder",
                initiator_id=initiator_id,
                responder_id=responder_id,
            )
            return

        # Forward via emit_to_user — covers local sids + cross-server
        # delivery via federation in one call. Returns 0 only when the
        # responder is fully offline across the whole Helen mesh.
        delivered = await emit_to_user(
            "e2ee:session_request",
            {
                "initiator_id": initiator_id,
                "key_agreement_data": key_agreement_data,
            },
            responder_id,
        )
        if delivered:
            logger.info(
                "e2ee_session_request_forwarded",
                initiator_id=initiator_id,
                responder_id=responder_id,
                delivered=delivered,
            )
        else:
            # Offline: store for later delivery
            # (Implement using notification service with pending_e2ee_session table if needed)
            logger.info(
                "e2ee_session_request_offline",
                initiator_id=initiator_id,
                responder_id=responder_id,
                note="Not implemented: implement via pending_e2ee_session table",
            )

    except Exception as e:
        logger.error(
            "e2ee_session_request_error",
            initiator_id=initiator_id,
            error=str(e),
        )


@sio.event
async def e2ee_session_ack(sid: str, data: dict[str, Any]):
    """
    Responder acknowledges encrypted session establishment.

    Called after responder completes X3DH and derives shared secret.

    Client sends:
      {
        "initiator_id": str,
        "session_id": str,  # Hash of initial DH outputs (same on both sides)
        "key_agreement_response": str,  # X3DH response message
      }

    Server forwards acknowledgment to initiator.

    Security:
      - Responder ID is extracted from socket (cannot be spoofed)
      - Ack contains derived session_id which both parties will match
      - If session_ids don't match, key agreement failed (abort)
    """
    responder_id = await get_user_id(sid)
    if not responder_id:
        logger.warning("e2ee_session_ack_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict):
            logger.warning("e2ee_session_ack_invalid", responder_id=responder_id)
            return

        initiator_id = data.get("initiator_id")
        session_id = data.get("session_id")
        key_agreement_response = data.get("key_agreement_response")

        if not initiator_id or not session_id or not key_agreement_response:
            logger.warning(
                "e2ee_session_ack_missing_fields",
                responder_id=responder_id,
                initiator_id=initiator_id,
            )
            return

        if not is_valid_uuid(initiator_id):
            logger.warning(
                "e2ee_session_ack_invalid_initiator",
                responder_id=responder_id,
                initiator_id=initiator_id,
            )
            return

        # Forward to initiator (covers local sids + federation fallback)
        await emit_to_user(
            "e2ee:session_ack",
            {
                "responder_id": responder_id,
                "session_id": session_id,
                "key_agreement_response": key_agreement_response,
            },
            initiator_id,
        )

        logger.info(
            "e2ee_session_ack_forwarded",
            initiator_id=initiator_id,
            responder_id=responder_id,
            session_id=session_id[:16],
        )

    except Exception as e:
        logger.error(
            "e2ee_session_ack_error",
            responder_id=responder_id,
            error=str(e),
        )


@sio.event
async def e2ee_pre_keys_low(sid: str, data: dict[str, Any]):
    """
    Server notifies user when their one-time pre-key supply is running low.

    This is a one-way notification (server -> client) sent after bundle fetch.
    Client should respond by uploading fresh one-time pre-keys.

    Sent by server (via REST endpoint) after consuming an OTP key, if remaining < 10.

    Server emits to user:
      {
        "remaining_pre_keys": int,
        "should_upload": true,
      }

    This handler is a client-side event listener (not a route).
    In practice, the server would emit this as an unsolicited event to the user.

    Example from service layer:
      if remaining_count < 10:
        for sid in presence_service.get_sids(user_id):
          await sio.emit("e2ee:pre_keys_low", {...}, to=sid)
    """
    # This is a listen-only event (client receives).
    # Implementation note: Server-side code should emit this after OTP consumption.
    pass
