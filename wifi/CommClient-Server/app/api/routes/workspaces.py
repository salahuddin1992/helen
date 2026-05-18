"""
Phase 3 / Module M — Workspace REST endpoints.

All routes mount under ``/api/workspaces``. The router is wired up by
``app.api.routes._phase3_routers.register_phase3_routers``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceInvite, WorkspaceMember
from app.services.tenancy import workspace_service as svc

logger = get_logger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


# ── Pydantic shapes ─────────────────────────────────────────

class WorkspaceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = None
    plan: str = "free"
    settings: dict[str, Any] | None = None


class WorkspaceUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = None
    plan: Optional[str] = None
    settings: Optional[dict[str, Any]] = None


class WorkspaceOut(BaseModel):
    id: str
    slug: str
    name: str
    description: Optional[str]
    plan: str
    owner_id: str
    is_active: bool
    settings: dict[str, Any]
    created_at: datetime
    member_count: int = 0
    role: Optional[str] = None      # current user's role, if known

    @classmethod
    def from_orm(cls, ws: Workspace, role: Optional[str] = None,
                 member_count: int = 0) -> "WorkspaceOut":
        return cls(
            id=ws.id, slug=ws.slug, name=ws.name,
            description=ws.description, plan=ws.plan,
            owner_id=ws.owner_id, is_active=ws.is_active,
            settings=ws.settings or {}, created_at=ws.created_at,
            member_count=member_count, role=role,
        )


class MemberAddIn(BaseModel):
    user_id: str
    role: str = "member"


class MemberOut(BaseModel):
    id: str
    user_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    role: str
    joined_at: datetime
    invited_by: Optional[str] = None


class InviteCreateIn(BaseModel):
    role: str = "member"
    email: Optional[str] = None
    ttl_hours: int = Field(default=72, ge=1, le=24 * 30)


class InviteOut(BaseModel):
    id: str
    code: str
    role: str
    email: Optional[str]
    issued_by: Optional[str]
    created_at: datetime
    expires_at: datetime
    used_at: Optional[datetime]
    used_by_id: Optional[str]


class AcceptInviteIn(BaseModel):
    code: str


# ── helpers ────────────────────────────────────────────────

async def _require_membership(
    db: AsyncSession, workspace_id: str, user_id: str,
    min_role: str = "member",
) -> WorkspaceMember:
    member = await svc.get_membership(db, workspace_id, user_id)
    if not member:
        raise HTTPException(status_code=403, detail="Not a member of this workspace.")
    hierarchy = ("viewer", "member", "admin", "owner")
    if hierarchy.index(member.role) < hierarchy.index(min_role):
        raise HTTPException(
            status_code=403,
            detail=f"This action requires the '{min_role}' role.",
        )
    return member


# ── Workspace CRUD ─────────────────────────────────────────

@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace_endpoint(
    body: WorkspaceCreateIn,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    try:
        ws = await svc.create_workspace(
            db, owner=user_id, name=body.name, slug=body.slug,
            description=body.description, plan=body.plan,
            settings=body.settings,
        )
        await db.commit()
    except svc.WorkspaceError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return WorkspaceOut.from_orm(ws, role="owner", member_count=1)


@router.get("", response_model=list[WorkspaceOut])
async def list_my_workspaces(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceOut]:
    rows = await svc.get_user_workspaces(db, user_id)
    out: list[WorkspaceOut] = []
    for ws, role in rows:
        member_count = await db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == ws.id)
        ) or 0
        out.append(WorkspaceOut.from_orm(ws, role=role, member_count=int(member_count)))
    return out


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace_endpoint(
    workspace_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    member = await _require_membership(db, workspace_id, user_id)
    ws = await svc.get_workspace(db, workspace_id)
    member_count = len(await svc.list_members(db, workspace_id))
    return WorkspaceOut.from_orm(ws, role=member.role, member_count=member_count)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace_endpoint(
    body: WorkspaceUpdateIn,
    workspace_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    member = await _require_membership(db, workspace_id, user_id, min_role="admin")
    ws = await svc.update_workspace_settings(
        db, workspace_id, name=body.name, description=body.description,
        settings=body.settings, plan=body.plan,
    )
    await db.commit()
    audit_log("workspace.updated", user_id=user_id, success=True,
              details={"workspace_id": workspace_id})
    member_count = len(await svc.list_members(db, workspace_id))
    return WorkspaceOut.from_orm(ws, role=member.role, member_count=member_count)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_endpoint(
    workspace_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await svc.delete_workspace(db, workspace_id, user_id)
        await db.commit()
    except svc.WorkspaceNotFound:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    except svc.WorkspacePermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return None


# ── Members ────────────────────────────────────────────────

@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_workspace_members(
    workspace_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[MemberOut]:
    await _require_membership(db, workspace_id, user_id)
    members = await svc.list_members(db, workspace_id)

    user_ids = [m.user_id for m in members]
    users_by_id: dict[str, User] = {}
    if user_ids:
        users = await db.scalars(select(User).where(User.id.in_(user_ids)))
        users_by_id = {u.id: u for u in users}

    out: list[MemberOut] = []
    for m in members:
        u = users_by_id.get(m.user_id)
        out.append(MemberOut(
            id=m.id, user_id=m.user_id,
            username=getattr(u, "username", None),
            display_name=getattr(u, "display_name", None),
            role=m.role, joined_at=m.joined_at, invited_by=m.invited_by,
        ))
    return out


@router.post("/{workspace_id}/members", response_model=MemberOut, status_code=201)
async def add_workspace_member(
    body: MemberAddIn,
    workspace_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> MemberOut:
    await _require_membership(db, workspace_id, user_id, min_role="admin")
    try:
        member = await svc.add_member(
            db, workspace_id, body.user_id, role=body.role, invited_by=user_id,
        )
        await db.commit()
    except svc.WorkspaceError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return MemberOut(
        id=member.id, user_id=member.user_id, role=member.role,
        joined_at=member.joined_at, invited_by=member.invited_by,
    )


@router.delete("/{workspace_id}/members/{member_user_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_workspace_member(
    workspace_id: str,
    member_user_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_membership(db, workspace_id, user_id, min_role="admin")
    try:
        removed = await svc.remove_member(db, workspace_id, member_user_id)
    except svc.WorkspacePermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="Member not found.")
    await db.commit()
    audit_log(
        "workspace.member_removed", user_id=user_id, success=True,
        details={"workspace_id": workspace_id, "target": member_user_id},
    )


# ── Invites ────────────────────────────────────────────────

@router.post("/{workspace_id}/invites", response_model=InviteOut, status_code=201)
async def create_workspace_invite(
    body: InviteCreateIn,
    workspace_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> InviteOut:
    await _require_membership(db, workspace_id, user_id, min_role="admin")
    try:
        inv = await svc.generate_invite(
            db, workspace_id, role=body.role, email=body.email,
            ttl_hours=body.ttl_hours, issued_by=user_id,
        )
        await db.commit()
    except svc.WorkspaceError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return InviteOut(
        id=inv.id, code=inv.code, role=inv.role, email=inv.email,
        issued_by=inv.issued_by, created_at=inv.created_at,
        expires_at=inv.expires_at, used_at=inv.used_at,
        used_by_id=inv.used_by_id,
    )


@router.get("/{workspace_id}/invites", response_model=list[InviteOut])
async def list_workspace_invites(
    workspace_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[InviteOut]:
    await _require_membership(db, workspace_id, user_id, min_role="admin")
    rows = await svc.list_invites(db, workspace_id)
    return [
        InviteOut(
            id=i.id, code=i.code, role=i.role, email=i.email,
            issued_by=i.issued_by, created_at=i.created_at,
            expires_at=i.expires_at, used_at=i.used_at,
            used_by_id=i.used_by_id,
        )
        for i in rows
    ]


@router.delete("/{workspace_id}/invites/{invite_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def revoke_workspace_invite(
    workspace_id: str,
    invite_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_membership(db, workspace_id, user_id, min_role="admin")
    ok = await svc.revoke_invite(db, invite_id, workspace_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Invite not found.")
    await db.commit()


@router.post("/invites/accept", response_model=MemberOut)
async def accept_invite_endpoint(
    body: AcceptInviteIn,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> MemberOut:
    user = await db.get(User, user_id)
    try:
        member = await svc.accept_invite(
            db, body.code, user_id,
            user_email=None,    # we don't track email on local User yet
        )
        await db.commit()
    except svc.WorkspacePermissionDenied as exc:
        await db.rollback()
        raise HTTPException(status_code=403, detail=str(exc))
    except svc.WorkspaceError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return MemberOut(
        id=member.id, user_id=member.user_id,
        username=getattr(user, "username", None),
        display_name=getattr(user, "display_name", None),
        role=member.role, joined_at=member.joined_at,
        invited_by=member.invited_by,
    )


# ── Admin-only catalogue ────────────────────────────────────

@router.get("/_admin/all", response_model=list[WorkspaceOut],
            dependencies=[Depends(require_role("admin"))])
async def admin_list_all_workspaces(
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceOut]:
    rows = await db.scalars(select(Workspace).order_by(Workspace.created_at.desc()))
    out: list[WorkspaceOut] = []
    for ws in rows:
        member_count = await db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == ws.id)
        ) or 0
        out.append(WorkspaceOut.from_orm(ws, role=None, member_count=int(member_count)))
    return out
