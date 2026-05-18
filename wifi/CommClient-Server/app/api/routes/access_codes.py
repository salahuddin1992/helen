"""
User-facing access code endpoints.

  POST   /api/me/codes         — mint a new code
  GET    /api/me/codes         — list my codes
  DELETE /api/me/codes/{code}  — revoke one of my codes
  POST   /api/codes/redeem     — redeem (consume) a code

Creation is rate-limited by the global rate limiter. Listing returns
only the authenticated caller's own codes. Redemption works cross-user.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.deps import get_current_user_id
from app.services.access_codes_service import get_service, VALID_KINDS

router = APIRouter(tags=["access-codes"])


class _CreateCode(BaseModel):
    kind:             str = Field(default="invite", description="invite | guest_auth | share")
    note:             str = ""
    max_uses:         Optional[int] = None
    ttl_sec:          Optional[int] = None
    target_channel_id: Optional[str] = None


@router.post("/me/codes", status_code=201)
async def create_my_code(
    body: _CreateCode,
    user_id: str = Depends(get_current_user_id),
):
    try:
        record = get_service().create(
            owner_user_id=user_id,
            kind=body.kind,
            note=body.note,
            max_uses=body.max_uses,
            ttl_sec=body.ttl_sec,
            target_channel_id=body.target_channel_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return record


@router.get("/me/codes")
async def list_my_codes(user_id: str = Depends(get_current_user_id)):
    return {"codes": get_service().list_by_owner(user_id)}


@router.delete("/me/codes/{code}", status_code=204)
async def revoke_my_code(
    code: str,
    user_id: str = Depends(get_current_user_id),
):
    try:
        ok = get_service().revoke(code, user_id)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not your code")
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="code not found")
    return None


class _RedeemCode(BaseModel):
    code: str


@router.post("/codes/redeem")
async def redeem_code(
    body: _RedeemCode,
    user_id: str = Depends(get_current_user_id),
):
    ok, reason, record = get_service().redeem(body.code, user_id)
    if not ok:
        # Map reason → HTTP status.
        status_map = {
            "not_found":              status.HTTP_404_NOT_FOUND,
            "expired":                status.HTTP_410_GONE,
            "revoked":                status.HTTP_410_GONE,
            "exhausted":              status.HTTP_410_GONE,
            "self_redeem_forbidden":  status.HTTP_400_BAD_REQUEST,
        }
        raise HTTPException(
            status_code=status_map.get(reason, status.HTTP_400_BAD_REQUEST),
            detail={"error": "redeem_failed", "reason": reason},
        )
    # Side-effects per-kind. For now we only return the record; the
    # actual "join this channel" action is the caller's follow-up call.
    return {"ok": True, "record": record}
