"""
Granular channel permissions REST endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.services.permission_service import (
    PERMISSIONS,
    PermissionService,
)

router = APIRouter(prefix="/channels/{channel_id}/permissions", tags=["permissions"])


class RolePermissionBody(BaseModel):
    role: str = Field(..., pattern="^(admin|moderator|member)$")
    permission: str
    granted: bool = True


class MemberPermissionBody(BaseModel):
    user_id: str
    permission: str
    granted: bool = True


@router.get("")
async def list_permissions(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return the full permission catalogue + the caller's effective perms."""
    effective = await PermissionService.effective_permissions(db, channel_id, user_id)
    role_overrides = await PermissionService.list_role_permissions(db, channel_id)
    return {
        "catalogue": list(PERMISSIONS),
        "effective": effective,
        "role_overrides": [
            {
                "id": r.id,
                "role": r.role,
                "permission": r.permission,
                "granted": r.granted,
            }
            for r in role_overrides
        ],
    }


@router.get("/me")
async def my_effective(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return {
        "channel_id": channel_id,
        "user_id": user_id,
        "effective": await PermissionService.effective_permissions(
            db, channel_id, user_id
        ),
    }


@router.put("/role")
async def set_role_perm(
    channel_id: str,
    body: RolePermissionBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await PermissionService.set_role_permission(
            db, channel_id, user_id, body.role, body.permission, body.granted
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Channel not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": rec.id,
        "role": rec.role,
        "permission": rec.permission,
        "granted": rec.granted,
    }


@router.delete("/role")
async def clear_role_perm(
    channel_id: str,
    body: RolePermissionBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        removed = await PermissionService.clear_role_permission(
            db, channel_id, user_id, body.role, body.permission
        )
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"removed": removed}


@router.put("/member")
async def set_member_perm(
    channel_id: str,
    body: MemberPermissionBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await PermissionService.set_member_permission(
            db, channel_id, user_id, body.user_id, body.permission, body.granted
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Member not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": rec.id,
        "user_id": rec.user_id,
        "permission": rec.permission,
        "granted": rec.granted,
    }


@router.delete("/member")
async def clear_member_perm(
    channel_id: str,
    body: MemberPermissionBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        removed = await PermissionService.clear_member_permission(
            db, channel_id, user_id, body.user_id, body.permission
        )
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"removed": removed}


@router.get("/member/{target_user_id}")
async def list_member_perms(
    channel_id: str,
    target_user_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    overrides = await PermissionService.list_member_permissions(
        db, channel_id, target_user_id
    )
    effective = await PermissionService.effective_permissions(
        db, channel_id, target_user_id
    )
    return {
        "user_id": target_user_id,
        "effective": effective,
        "overrides": [
            {"permission": o.permission, "granted": o.granted} for o in overrides
        ],
    }
