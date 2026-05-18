"""
Phase 3 / Module M — Workspace service layer.

All workspace lifecycle (create / member CRUD / invite generation /
invite acceptance / lookup) lives here so endpoints stay thin and
socket handlers can reuse the same primitives.

Concurrency: every public coroutine is fully transactional on the
caller-supplied ``AsyncSession``. The caller is responsible for
``commit()`` — services NEVER commit hidden transactions so they can
be composed inside larger units of work.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.db.base import utc_now
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceInvite, WorkspaceMember

logger = get_logger(__name__)

VALID_ROLES = ("owner", "admin", "member", "viewer")
MAX_INVITES_PER_HOUR = 25
MAX_WORKSPACES_PER_USER = 50


class WorkspaceError(Exception):
    """Raised for any user-visible workspace failure."""


class WorkspaceNotFound(WorkspaceError):
    pass


class WorkspacePermissionDenied(WorkspaceError):
    pass


# ── Slug helpers ────────────────────────────────────────────

def _normalize_slug(raw: str) -> str:
    """Lowercase, replace non-alnum with '-', collapse runs, strip edges."""
    out: list[str] = []
    prev_dash = False
    for ch in raw.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return slug[:60] or secrets.token_hex(6)


async def _slug_unique(db: AsyncSession, slug: str) -> str:
    """Return slug or slug-<n> to guarantee uniqueness."""
    candidate = slug
    suffix = 1
    while True:
        exists = await db.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.slug == candidate)
        )
        if not exists:
            return candidate
        suffix += 1
        candidate = f"{slug}-{suffix}"
        if suffix > 1000:                                       # pragma: no cover
            return f"{slug}-{secrets.token_hex(3)}"


# ── Workspace lifecycle ─────────────────────────────────────

async def create_workspace(
    db: AsyncSession,
    owner: User | str,
    name: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    plan: str = "free",
    settings: Optional[dict] = None,
) -> Workspace:
    """Create a workspace. Caller must commit. Owner auto-added as member."""
    owner_id = owner.id if isinstance(owner, User) else owner
    if not owner_id:
        raise WorkspaceError("owner_id required")

    name = (name or "").strip()
    if not name:
        raise WorkspaceError("name required")

    # Enforce per-user workspace cap.
    owned_count = await db.scalar(
        select(func.count()).select_from(Workspace).where(Workspace.owner_id == owner_id)
    ) or 0
    if owned_count >= MAX_WORKSPACES_PER_USER:
        raise WorkspaceError(
            f"You have reached the {MAX_WORKSPACES_PER_USER}-workspace limit."
        )

    candidate_slug = _normalize_slug(slug or name)
    candidate_slug = await _slug_unique(db, candidate_slug)

    ws = Workspace(
        slug=candidate_slug,
        name=name,
        description=description,
        owner_id=owner_id,
        plan=plan or "free",
        settings=settings or {},
    )
    db.add(ws)
    await db.flush()

    db.add(WorkspaceMember(
        workspace_id=ws.id,
        user_id=owner_id,
        role="owner",
        invited_by=owner_id,
    ))
    await db.flush()

    audit_log(
        "workspace.created",
        user_id=owner_id,
        success=True,
        details={"workspace_id": ws.id, "slug": ws.slug},
    )
    return ws


async def get_workspace(db: AsyncSession, workspace_id: str) -> Workspace:
    ws = await db.get(Workspace, workspace_id)
    if not ws:
        raise WorkspaceNotFound(workspace_id)
    return ws


async def get_workspace_by_slug(db: AsyncSession, slug: str) -> Optional[Workspace]:
    return await db.scalar(select(Workspace).where(Workspace.slug == slug))


async def update_workspace_settings(
    db: AsyncSession,
    workspace_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    settings: Optional[dict] = None,
    plan: Optional[str] = None,
) -> Workspace:
    ws = await get_workspace(db, workspace_id)
    if name is not None:
        ws.name = name.strip() or ws.name
    if description is not None:
        ws.description = description
    if plan is not None:
        ws.plan = plan
    if settings is not None:
        merged = dict(ws.settings or {})
        merged.update(settings)
        ws.settings = merged
    return ws


async def delete_workspace(
    db: AsyncSession, workspace_id: str, requester_id: str,
) -> None:
    ws = await get_workspace(db, workspace_id)
    if ws.owner_id != requester_id:
        raise WorkspacePermissionDenied("Only the owner can delete a workspace.")
    await db.delete(ws)
    audit_log(
        "workspace.deleted", user_id=requester_id,
        success=True, details={"workspace_id": workspace_id},
    )


# ── Members ─────────────────────────────────────────────────

async def add_member(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
    role: str = "member",
    invited_by: Optional[str] = None,
) -> WorkspaceMember:
    if role not in VALID_ROLES:
        raise WorkspaceError(f"invalid role: {role!r}")

    # idempotent: return existing membership if present
    existing = await db.scalar(
        select(WorkspaceMember).where(
            and_(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
    )
    if existing:
        if existing.role != role:
            existing.role = role
        return existing

    member = WorkspaceMember(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role,
        invited_by=invited_by,
    )
    db.add(member)
    try:
        await db.flush()
    except IntegrityError as exc:                              # pragma: no cover
        await db.rollback()
        raise WorkspaceError("duplicate membership") from exc
    return member


async def remove_member(
    db: AsyncSession, workspace_id: str, user_id: str,
) -> bool:
    member = await db.scalar(
        select(WorkspaceMember).where(
            and_(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
    )
    if not member:
        return False
    if member.role == "owner":
        raise WorkspacePermissionDenied("Cannot remove the workspace owner.")
    await db.delete(member)
    return True


async def list_members(
    db: AsyncSession, workspace_id: str,
) -> list[WorkspaceMember]:
    rows = await db.scalars(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id
        ).order_by(WorkspaceMember.joined_at.asc())
    )
    return list(rows)


async def get_membership(
    db: AsyncSession, workspace_id: str, user_id: str,
) -> Optional[WorkspaceMember]:
    return await db.scalar(
        select(WorkspaceMember).where(
            and_(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
    )


async def get_user_workspaces(
    db: AsyncSession, user_id: str,
) -> list[tuple[Workspace, str]]:
    """Return list of (workspace, role) tuples for a user."""
    rows = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.created_at.asc())
    )
    return [(ws, role) for ws, role in rows.all()]


# ── Invites ─────────────────────────────────────────────────

async def generate_invite(
    db: AsyncSession,
    workspace_id: str,
    role: str = "member",
    email: Optional[str] = None,
    ttl_hours: int = 72,
    issued_by: Optional[str] = None,
) -> WorkspaceInvite:
    if role not in VALID_ROLES:
        raise WorkspaceError(f"invalid role: {role!r}")

    # Rate limit per workspace.
    if issued_by:
        since = utc_now() - timedelta(hours=1)
        recent = await db.scalar(
            select(func.count()).select_from(WorkspaceInvite).where(
                and_(
                    WorkspaceInvite.workspace_id == workspace_id,
                    WorkspaceInvite.issued_by == issued_by,
                    WorkspaceInvite.created_at >= since,
                )
            )
        ) or 0
        if recent >= MAX_INVITES_PER_HOUR:
            raise WorkspaceError("invite rate-limit reached, try again later.")

    invite = WorkspaceInvite(
        workspace_id=workspace_id,
        role=role,
        email=(email or None),
        issued_by=issued_by,
        expires_at=utc_now() + timedelta(hours=max(1, ttl_hours)),
    )
    db.add(invite)
    await db.flush()
    audit_log(
        "workspace.invite_generated", user_id=issued_by,
        success=True,
        details={"workspace_id": workspace_id, "invite_id": invite.id},
    )
    return invite


async def list_invites(
    db: AsyncSession, workspace_id: str,
) -> list[WorkspaceInvite]:
    rows = await db.scalars(
        select(WorkspaceInvite).where(
            WorkspaceInvite.workspace_id == workspace_id
        ).order_by(WorkspaceInvite.created_at.desc())
    )
    return list(rows)


async def revoke_invite(
    db: AsyncSession, invite_id: str, workspace_id: str,
) -> bool:
    inv = await db.get(WorkspaceInvite, invite_id)
    if not inv or inv.workspace_id != workspace_id:
        return False
    await db.delete(inv)
    return True


async def accept_invite(
    db: AsyncSession,
    code: str,
    user_id: str,
    user_email: Optional[str] = None,
) -> WorkspaceMember:
    inv = await db.scalar(select(WorkspaceInvite).where(WorkspaceInvite.code == code))
    if not inv:
        raise WorkspaceError("Invite not found.")
    if inv.is_consumed:
        raise WorkspaceError("Invite already used.")
    if inv.is_expired:
        raise WorkspaceError("Invite expired.")
    if inv.email and user_email and inv.email.lower() != user_email.lower():
        raise WorkspacePermissionDenied("This invite is bound to a different email.")

    member = await add_member(
        db, inv.workspace_id, user_id, role=inv.role, invited_by=inv.issued_by,
    )
    inv.used_at = utc_now()
    inv.used_by_id = user_id
    audit_log(
        "workspace.invite_accepted",
        user_id=user_id, success=True,
        details={"workspace_id": inv.workspace_id, "invite_id": inv.id},
    )
    return member


# ── FastAPI dependency ──────────────────────────────────────

async def current_workspace_dependency(
    x_workspace_id: Annotated[Optional[str], Header(alias="X-Workspace-Id")] = None,
    x_workspace_slug: Annotated[Optional[str], Header(alias="X-Workspace-Slug")] = None,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    """Resolve the current workspace from headers and validate that the
    authenticated user is a member. ``X-Workspace-Id`` wins over
    ``X-Workspace-Slug``. If neither is sent and the user only has ONE
    workspace, we auto-pick it (convenience for single-tenant clients)."""
    ws: Optional[Workspace] = None
    if x_workspace_id:
        ws = await db.get(Workspace, x_workspace_id)
    elif x_workspace_slug:
        ws = await get_workspace_by_slug(db, x_workspace_slug)
    else:
        memberships = await get_user_workspaces(db, user_id)
        if len(memberships) == 1:
            ws = memberships[0][0]

    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workspace not specified — set X-Workspace-Id header.",
        )

    membership = await get_membership(db, ws.id, user_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this workspace.",
        )
    return ws
