"""
Admin — Granular RBAC management (Phase 2 / Module G).

Endpoints
---------
GET    /api/admin/rbac/roles                       list roles
POST   /api/admin/rbac/roles                       create role
GET    /api/admin/rbac/roles/{id}                  role detail with perms
PUT    /api/admin/rbac/roles/{id}                  rename / re-describe (system roles: only description)
DELETE /api/admin/rbac/roles/{id}                  delete (forbid system roles)
GET    /api/admin/rbac/permissions                 catalogue
POST   /api/admin/rbac/roles/{id}/permissions      bulk replace
GET    /api/admin/rbac/users/{user_id}/roles       list assigned roles
POST   /api/admin/rbac/users/{user_id}/roles       assign role
DELETE /api/admin/rbac/users/{user_id}/roles/{rid} unassign
GET    /api/admin/rbac/users/{user_id}/effective   flattened permission set
POST   /api/admin/rbac/bootstrap                   re-run the seed (safe to repeat)

All write endpoints require ``rbac.roles_write`` or ``rbac.permissions_assign``.
Reads require ``rbac.roles_read``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services.rbac import enforcer
from app.services.rbac.enforcer import require_permission
from app.services.rbac.registry import (
    PERMISSION_TREE,
    SUPERADMIN_ROLE_NAME,
    bootstrap_default_roles,
    is_valid_permission,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/rbac", tags=["admin-phase2"])


# ── Pydantic shapes ───────────────────────────────────────

class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = None


class RoleOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    is_system: bool
    created_at: datetime
    permissions: list[str] = []
    user_count: int = 0


class PermissionOut(BaseModel):
    id: str
    key: str
    category: str
    description: Optional[str]


class PermissionsAssign(BaseModel):
    """Replace the role's permissions with this list."""
    permissions: list[str]


class UserRoleAssign(BaseModel):
    role_id: str


# ── Helpers ───────────────────────────────────────────────

async def _load_role_with_perms(db: AsyncSession, role_id: str) -> Role:
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


async def _role_permission_keys(db: AsyncSession, role_id: str) -> list[str]:
    rows = (await db.execute(
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id, RolePermission.granted == True)  # noqa: E712
    )).all()
    return sorted(r[0] for r in rows)


async def _user_count_for_role(db: AsyncSession, role_id: str) -> int:
    rows = (await db.execute(
        select(UserRole.user_id).where(UserRole.role_id == role_id)
    )).all()
    return len(rows)


# ── Role CRUD ─────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    user_id: str = Depends(require_permission("rbac.roles_read")),
    db: AsyncSession = Depends(get_db),
):
    roles = (await db.execute(select(Role).order_by(Role.name))).scalars().all()
    out: list[RoleOut] = []
    for r in roles:
        out.append(RoleOut(
            id=r.id, name=r.name, description=r.description,
            is_system=r.is_system, created_at=r.created_at,
            permissions=await _role_permission_keys(db, r.id),
            user_count=await _user_count_for_role(db, r.id),
        ))
    return out


@router.post("/roles", response_model=RoleOut, status_code=201)
async def create_role(
    body: RoleCreate,
    user_id: str = Depends(require_permission("rbac.roles_write")),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    exists = (await db.execute(
        select(Role).where(Role.name == name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="role exists")
    role = Role(name=name, description=body.description, is_system=False)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    audit_log("rbac.role_created", user_id=user_id, success=True,
              details={"name": name})
    return RoleOut(
        id=role.id, name=role.name, description=role.description,
        is_system=role.is_system, created_at=role.created_at,
        permissions=[], user_count=0,
    )


@router.get("/roles/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: str,
    user_id: str = Depends(require_permission("rbac.roles_read")),
    db: AsyncSession = Depends(get_db),
):
    role = await _load_role_with_perms(db, role_id)
    return RoleOut(
        id=role.id, name=role.name, description=role.description,
        is_system=role.is_system, created_at=role.created_at,
        permissions=await _role_permission_keys(db, role.id),
        user_count=await _user_count_for_role(db, role.id),
    )


@router.put("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    body: RoleUpdate,
    user_id: str = Depends(require_permission("rbac.roles_write")),
    db: AsyncSession = Depends(get_db),
):
    role = await _load_role_with_perms(db, role_id)
    if role.is_system and body.name and body.name != role.name:
        raise HTTPException(status_code=400, detail="cannot rename a system role")
    if body.name:
        role.name = body.name.strip()
    if body.description is not None:
        role.description = body.description
    await db.commit()
    await db.refresh(role)
    audit_log("rbac.role_updated", user_id=user_id, success=True,
              details={"role_id": role.id})
    await enforcer.invalidate_all()
    return RoleOut(
        id=role.id, name=role.name, description=role.description,
        is_system=role.is_system, created_at=role.created_at,
        permissions=await _role_permission_keys(db, role.id),
        user_count=await _user_count_for_role(db, role.id),
    )


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: str,
    user_id: str = Depends(require_permission("rbac.roles_write")),
    db: AsyncSession = Depends(get_db),
):
    role = await _load_role_with_perms(db, role_id)
    if role.is_system:
        raise HTTPException(status_code=400, detail="cannot delete a system role")
    await db.delete(role)
    await db.commit()
    audit_log("rbac.role_deleted", user_id=user_id, success=True,
              details={"role_id": role_id, "name": role.name})
    await enforcer.invalidate_all()


# ── Permission catalogue ──────────────────────────────────

@router.get("/permissions")
async def list_permissions(
    user_id: str = Depends(require_permission("rbac.roles_read")),
    db: AsyncSession = Depends(get_db),
):
    perms = (await db.execute(
        select(Permission).order_by(Permission.category, Permission.key)
    )).scalars().all()
    return {
        "tree": PERMISSION_TREE,
        "permissions": [
            PermissionOut(
                id=p.id, key=p.key, category=p.category,
                description=p.description,
            ).model_dump()
            for p in perms
        ],
    }


@router.post("/roles/{role_id}/permissions")
async def replace_role_permissions(
    role_id: str,
    body: PermissionsAssign,
    user_id: str = Depends(require_permission("rbac.permissions_assign")),
    db: AsyncSession = Depends(get_db),
):
    role = await _load_role_with_perms(db, role_id)

    # Validate keys
    bad = [k for k in body.permissions if not is_valid_permission(k)]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"unknown permission keys: {bad}")

    # Wipe existing, then insert
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    if body.permissions:
        perm_rows = (await db.execute(
            select(Permission).where(Permission.key.in_(body.permissions))
        )).scalars().all()
        for p in perm_rows:
            db.add(RolePermission(role_id=role.id, permission_id=p.id, granted=True))
    await db.commit()
    audit_log("rbac.role_permissions_replaced", user_id=user_id, success=True,
              details={"role_id": role.id, "count": len(body.permissions)})
    await enforcer.invalidate_all()
    return {"role_id": role.id, "permissions": sorted(body.permissions)}


# ── User-role assignments ─────────────────────────────────

@router.get("/users/{target_user_id}/roles")
async def list_user_roles(
    target_user_id: str,
    user_id: str = Depends(require_permission("rbac.roles_read")),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Role, UserRole)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == target_user_id)
    )).all()
    return {
        "user_id": target_user_id,
        "roles": [
            {
                "role_id": role.id,
                "name": role.name,
                "description": role.description,
                "is_system": role.is_system,
                "assigned_at": ur.assigned_at,
                "assigned_by": ur.assigned_by,
            }
            for role, ur in rows
        ],
    }


@router.post("/users/{target_user_id}/roles")
async def assign_user_role(
    target_user_id: str,
    body: UserRoleAssign,
    user_id: str = Depends(require_permission("rbac.permissions_assign")),
    db: AsyncSession = Depends(get_db),
):
    # Resolve target user + role
    target = await db.get(User, target_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    role = await db.get(Role, body.role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")

    exists = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == target_user_id,
            UserRole.role_id == body.role_id,
        )
    )).scalar_one_or_none()
    if exists:
        return {"ok": True, "already_assigned": True}

    db.add(UserRole(
        user_id=target_user_id, role_id=body.role_id,
        assigned_by=user_id,
    ))
    await db.commit()
    audit_log("rbac.role_assigned", user_id=user_id, success=True,
              details={"target": target_user_id, "role": role.name})
    await enforcer.invalidate(target_user_id)
    return {"ok": True, "user_id": target_user_id, "role": role.name}


@router.delete("/users/{target_user_id}/roles/{role_id}", status_code=204)
async def unassign_user_role(
    target_user_id: str,
    role_id: str,
    user_id: str = Depends(require_permission("rbac.permissions_assign")),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(delete(UserRole).where(
        UserRole.user_id == target_user_id,
        UserRole.role_id == role_id,
    ))
    await db.commit()
    audit_log("rbac.role_unassigned", user_id=user_id, success=True,
              details={"target": target_user_id, "role_id": role_id})
    await enforcer.invalidate(target_user_id)


@router.get("/users/{target_user_id}/effective")
async def effective_permissions(
    target_user_id: str,
    user_id: str = Depends(require_permission("rbac.roles_read")),
    db: AsyncSession = Depends(get_db),
):
    perms = await enforcer.get_user_permissions(db, target_user_id)
    legacy = (await db.execute(
        select(User.role).where(User.id == target_user_id)
    )).scalar_one_or_none()
    roles = (await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == target_user_id)
    )).all()
    return {
        "user_id": target_user_id,
        "legacy_role": legacy,
        "assigned_roles": [r[0] for r in roles],
        "permissions": sorted(perms),
        "is_superadmin": SUPERADMIN_ROLE_NAME in {r[0] for r in roles},
    }


@router.post("/bootstrap")
async def bootstrap(
    user_id: str = Depends(require_permission("rbac.roles_write")),
    db: AsyncSession = Depends(get_db),
):
    """Re-run the default-role bootstrap. Idempotent — safe to call repeatedly.
    Use after an upgrade adds new built-in permissions."""
    out = await bootstrap_default_roles(db)
    await db.commit()
    audit_log("rbac.bootstrap", user_id=user_id, success=True,
              details={"roles": list(out.keys())})
    await enforcer.invalidate_all()
    return {"ok": True, "roles": out}
