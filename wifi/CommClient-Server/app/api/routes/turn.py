"""
TURN relay management endpoints.

Endpoints:
- GET /turn/credentials — Generate temporary TURN credentials
- POST /turn/allocations — Create relay allocation
- GET /turn/allocations/{id} — Get allocation details
- POST /turn/allocations/{id}/refresh — Refresh allocation lifetime
- DELETE /turn/allocations/{id} — Delete allocation
- POST /turn/allocations/{id}/permissions — Add permission
- POST /turn/allocations/{id}/channels — Bind channel
- GET /turn/stats — Get TURN service statistics
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.core.deps import get_current_user_id
from app.core.logging import get_logger
from app.schemas.turn import (
    ChannelBindRequest,
    PermissionRequest,
    TURNAllocationDetail,
    TURNAllocationRequest,
    TURNAllocationResponse,
    TURNCredentialsRequest,
    TURNCredentialsResponse,
    TURNStats,
)
from app.services.ice_config_service import build_ice_config
from app.services.turn_service import turn_service

logger = get_logger(__name__)

router = APIRouter(prefix="/turn", tags=["turn"])


@router.get("/ice-config")
async def get_ice_config(
    ttl_seconds: int | None = Query(
        default=None, ge=60, le=86400,
        description="Override credential TTL (seconds). Default = server config.",
    ),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """
    Return a ready-to-use ``RTCConfiguration``-shaped payload.

    Response:
        {
            "ice_servers": [
                {"urls": ["stun:..."]},
                {"urls": ["turn:...", "turn:...?transport=tcp"],
                 "username": "...", "credential": "..."}
            ],
            "ice_transport_policy": "all" | "relay",
            "ttl_seconds": 3600,
            "realm": "commclient.local"
        }

    Clients should refresh before ``ttl_seconds`` elapses.

    **Authentication:** Requires valid JWT token.
    """
    try:
        return build_ice_config(user_id, ttl_seconds=ttl_seconds)
    except Exception as e:
        logger.error("ice_config_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to build ICE config")


@router.post("/credentials", response_model=TURNCredentialsResponse, status_code=201)
async def generate_credentials(
    body: TURNCredentialsRequest,
    user_id: str = Depends(get_current_user_id),
) -> TURNCredentialsResponse:
    """
    Generate ephemeral TURN credentials.

    Credentials use short-term authentication (RFC 5766).
    Password is HMAC-SHA1(secret, credential_username).

    Usage by client:
    1. Call this endpoint to get username and password
    2. Use credentials in TURN_SERVERS array for WebRTC configuration
    3. Credentials expire after ttl_seconds

    **Authentication:** Requires valid JWT token
    """
    try:
        creds = turn_service.generate_credentials(
            username=user_id,
            ttl_seconds=body.ttl_seconds,
        )
        logger.info(
            "turn_credentials_requested",
            user_id=user_id,
            ttl_seconds=body.ttl_seconds,
        )
        return TURNCredentialsResponse(**creds)
    except Exception as e:
        logger.error("turn_credentials_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to generate credentials")


@router.post("/allocations", response_model=TURNAllocationResponse, status_code=201)
async def create_allocation(
    body: TURNAllocationRequest,
    user_id: str = Depends(get_current_user_id),
) -> TURNAllocationResponse:
    """
    Create TURN relay allocation.

    Allocates a relay port and associates it with the client's transport address.
    Requires valid TURN credentials.

    The relay address can then be used in ICE candidates for WebRTC connectivity.

    **Authentication:** Requires valid JWT token
    """
    try:
        allocation = await turn_service.create_allocation(
            username=body.username,
            password=body.password,
            client_ip=body.client_ip,
            client_port=body.client_port,
            transport=body.transport,
            lifetime=body.lifetime,
        )

        logger.info(
            "turn_allocation_requested",
            user_id=user_id,
            allocation_id=allocation.allocation_id,
        )

        return TURNAllocationResponse(
            allocation_id=allocation.allocation_id,
            relay_ip=allocation.relay_ip,
            relay_port=allocation.relay_port,
            username=allocation.username,
            lifetime=allocation.lifetime_seconds,
            transport=allocation.transport,
        )

    except ValueError as e:
        logger.warning("turn_allocation_invalid", error=str(e), user_id=user_id)
        raise HTTPException(status_code=401, detail="Invalid or expired credentials")
    except RuntimeError as e:
        logger.error("turn_allocation_no_ports", error=str(e), user_id=user_id)
        raise HTTPException(status_code=503, detail="No available relay ports")
    except Exception as e:
        logger.error("turn_allocation_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to create allocation")


@router.get("/allocations/{allocation_id}", response_model=TURNAllocationDetail)
async def get_allocation(
    allocation_id: str,
    user_id: str = Depends(get_current_user_id),
) -> TURNAllocationDetail:
    """Get allocation details."""
    try:
        allocation = await turn_service.get_allocation(allocation_id)
        if not allocation:
            logger.warning(
                "turn_allocation_not_found",
                allocation_id=allocation_id,
                user_id=user_id,
            )
            raise HTTPException(status_code=404, detail="Allocation not found")

        return TURNAllocationDetail(**allocation.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error("turn_allocation_get_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve allocation")


@router.post("/allocations/{allocation_id}/refresh", response_model=TURNAllocationDetail)
async def refresh_allocation(
    allocation_id: str,
    lifetime: int = Query(600, ge=60, le=3600),
    user_id: str = Depends(get_current_user_id),
) -> TURNAllocationDetail:
    """Refresh allocation lifetime (extend expiry)."""
    try:
        allocation = await turn_service.refresh_allocation(allocation_id, lifetime)
        if not allocation:
            logger.warning(
                "turn_allocation_refresh_not_found",
                allocation_id=allocation_id,
                user_id=user_id,
            )
            raise HTTPException(status_code=404, detail="Allocation not found")

        logger.info(
            "turn_allocation_refreshed",
            allocation_id=allocation_id,
            user_id=user_id,
            lifetime=lifetime,
        )

        return TURNAllocationDetail(**allocation.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error("turn_allocation_refresh_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to refresh allocation")


@router.delete("/allocations/{allocation_id}", status_code=204, response_class=Response)
async def delete_allocation(
    allocation_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """Delete allocation and release relay port."""
    try:
        deleted = await turn_service.delete_allocation(allocation_id)
        if not deleted:
            logger.warning(
                "turn_allocation_delete_not_found",
                allocation_id=allocation_id,
                user_id=user_id,
            )
            raise HTTPException(status_code=404, detail="Allocation not found")

        logger.info(
            "turn_allocation_deleted",
            allocation_id=allocation_id,
            user_id=user_id,
        )
        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("turn_allocation_delete_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to delete allocation")


@router.post("/allocations/{allocation_id}/permissions", status_code=201)
async def add_permission(
    allocation_id: str,
    body: PermissionRequest,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Add or refresh permission for a peer address."""
    try:
        success = await turn_service.add_permission(
            allocation_id,
            body.peer_ip,
            body.peer_port,
            body.lifetime,
        )

        if not success:
            logger.warning(
                "turn_permission_failed",
                allocation_id=allocation_id,
                peer=f"{body.peer_ip}:{body.peer_port}",
                user_id=user_id,
            )
            raise HTTPException(status_code=404, detail="Allocation not found")

        logger.info(
            "turn_permission_added",
            allocation_id=allocation_id,
            user_id=user_id,
        )

        return {
            "status": "ok",
            "allocation_id": allocation_id,
            "peer": f"{body.peer_ip}:{body.peer_port}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("turn_permission_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to add permission")


@router.post("/allocations/{allocation_id}/channels", status_code=201)
async def bind_channel(
    allocation_id: str,
    body: ChannelBindRequest,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """
    Bind channel number to peer address.

    Channel binding provides optimized relay (RFC 5766 Section 11).
    Data on the channel can be relayed without explicit permission checks.
    """
    try:
        success = await turn_service.bind_channel(
            allocation_id,
            body.channel_number,
            body.peer_ip,
            body.peer_port,
        )

        if not success:
            logger.warning(
                "turn_channel_bind_failed",
                allocation_id=allocation_id,
                channel=hex(body.channel_number),
                user_id=user_id,
            )
            raise HTTPException(status_code=400, detail="Failed to bind channel")

        logger.info(
            "turn_channel_bound",
            allocation_id=allocation_id,
            user_id=user_id,
        )

        return {
            "status": "ok",
            "allocation_id": allocation_id,
            "channel": hex(body.channel_number),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("turn_channel_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to bind channel")


@router.get("/stats", response_model=TURNStats)
async def get_stats(
    user_id: str = Depends(get_current_user_id),
) -> TURNStats:
    """Get TURN service statistics and active allocations."""
    try:
        stats = await turn_service.get_stats()
        logger.info("turn_stats_requested", user_id=user_id)
        return TURNStats(**stats)
    except Exception as e:
        logger.error("turn_stats_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve statistics")
