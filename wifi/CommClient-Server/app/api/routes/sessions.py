"""
Session / device management REST endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.schemas.session import SessionListResponse, SessionResponse
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    sessions = await SessionService.list_sessions(db, user_id)
    return SessionListResponse(
        sessions=[SessionResponse.model_validate(s) for s in sessions],
        total=len(sessions),
    )


@router.delete("/{session_id}", status_code=204, response_class=Response)
async def revoke_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await SessionService.revoke_session(db, user_id, session_id)
    return Response(status_code=204)


@router.post("/revoke-all")
async def revoke_all_sessions(
    except_current: str | None = Query(None, description="Session ID to keep active"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    count = await SessionService.revoke_all_sessions(db, user_id, except_current)
    return {"revoked": count}
