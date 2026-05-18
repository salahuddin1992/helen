"""
Module L — Helen Agent REST + WebSocket router.

Mounted under the ``/api`` prefix by ``_phase3_agents_wireup.register_agents_router``.

Endpoints (Path / Method / Auth):
    POST    /api/agents/register                           public  — pair a new device
    POST    /api/agents/auth/refresh                       agent   — refresh → access exchange
    POST    /api/agents/{id}/heartbeat                     agent   — periodic snapshot push
    GET     /api/agents                                    admin   — paginated list
    GET     /api/agents/{id}                               admin   — full detail
    POST    /api/agents/{id}/command                       admin   — queue a whitelisted command
    GET     /api/agents/{id}/commands                      admin   — command history
    POST    /api/agents/{id}/files/upload                  agent   — agent uploads a file
    GET     /api/agents/{id}/files/download/{token}        agent   — agent downloads
    WS      /api/agents/{id}/control                       agent   — bi-directional control channel
    WS      /api/agents/{id}/events                        admin   — admin live event feed
    GET     /api/agents/update/manifest                    agent   — latest binary metadata
    GET     /api/agents/update/binary                      agent   — latest binary blob
    DELETE  /api/agents/{id}                               admin   — revoke + soft-delete

Admin endpoints require permission ``agents.manage`` (resolved through the
RBAC enforcer added in Module G). Until the permission catalogue is migrated
to include this key, the enforcer falls back to legacy ``admin`` role.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security import create_access_token, decode_token
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentCommand, AgentEvent
from app.services.agents.command_dispatcher import get_dispatcher
from app.services.agents.manager import get_agent_manager

logger = get_logger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])

_bearer = HTTPBearer(auto_error=False)


# ─────────────────────────────────────────────────────────────────────────────
# Permission gating helper
# ─────────────────────────────────────────────────────────────────────────────


async def require_agents_admin(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    if creds is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    payload = decode_token(creds.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="invalid token type")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="token has no subject")

    # Permission-first check
    try:
        from app.services.rbac.enforcer import user_has_permission
        if await user_has_permission(db, user_id, "agents.manage"):
            return user_id
    except Exception:
        pass
    # Legacy fallback — accept admin / superadmin role claim
    role = (payload.get("role") or "").lower()
    if role in {"admin", "superadmin"}:
        return user_id
    raise HTTPException(status_code=403, detail="missing permission: agents.manage")


async def require_agent_session(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict[str, Any]:
    """Returns the agent token payload."""
    if creds is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    payload = decode_token(creds.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="invalid token type")
    if not payload.get("agent_id"):
        raise HTTPException(status_code=403, detail="token is not an agent token")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    hostname: str = Field(min_length=1, max_length=256)
    os_name: Optional[str] = Field(default=None, max_length=64)
    os_version: Optional[str] = Field(default=None, max_length=128)
    agent_version: Optional[str] = Field(default=None, max_length=32)


class RegisterResponse(BaseModel):
    agent_id: str
    refresh_token: str
    message: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=8, max_length=512)
    agent_id: Optional[str] = None


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: Optional[str] = None


class CommandRequestBody(BaseModel):
    command: str = Field(min_length=1, max_length=128)
    args: list[str] = Field(default_factory=list, max_length=64)
    timeout_secs: int = Field(default=30, ge=1, le=300)


class AgentListItem(BaseModel):
    id: str
    fingerprint: str
    hostname: str
    os_name: Optional[str]
    os_version: Optional[str]
    agent_version: Optional[str]
    status: str
    last_heartbeat_at: Optional[str]
    registered_at: str
    last_ip: Optional[str]
    online_now: bool


class AgentListResponse(BaseModel):
    items: list[AgentListItem]
    total: int
    page: int
    page_size: int


# ─────────────────────────────────────────────────────────────────────────────
# Public registration & refresh
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/register", response_model=RegisterResponse)
async def register_agent(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """First-time pairing. Returns the new agent_id plus a refresh token.

    Idempotent on `fingerprint` — re-registering an existing device rotates
    its refresh token but keeps the same `agent_id`.
    """
    mgr = get_agent_manager()
    ip = request.client.host if request.client else None
    result = await mgr.register_agent(
        db,
        fingerprint=body.fingerprint,
        hostname=body.hostname,
        os_name=body.os_name,
        os_version=body.os_version,
        agent_version=body.agent_version,
        ip=ip,
    )
    return RegisterResponse(
        agent_id=result.agent.id,
        refresh_token=result.refresh_token,
        message="registered",
    )


@router.post("/auth/refresh", response_model=AccessTokenResponse)
async def refresh_agent_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> AccessTokenResponse:
    mgr = get_agent_manager()
    agent = await mgr.verify_refresh(
        db, agent_id=body.agent_id, refresh_token=body.refresh_token,
    )
    if not agent:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    access = create_access_token(
        user_id=f"agent:{agent.id}",
        role="agent",
        extra={"agent_id": agent.id, "fpr": agent.fingerprint[:16]},
    )
    return AccessTokenResponse(access_token=access, expires_in=3600)


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/{agent_id}/heartbeat")
async def heartbeat(
    agent_id: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    session: dict[str, Any] = Depends(require_agent_session),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if session.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="token / path mismatch")
    mgr = get_agent_manager()
    ip = request.client.host if request.client else None
    try:
        agent = await mgr.record_heartbeat(db, agent_id, payload, ip)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown agent")
    return {
        "status": agent.status,
        "next_heartbeat_in": 30,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin — listing & detail
# ─────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=AgentListResponse)
async def list_agents(
    _admin: str = Depends(require_agents_admin),
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=50, ge=1, le=200),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    search: Optional[str] = Query(default=None, max_length=128),
) -> AgentListResponse:
    base = select(Agent).where(Agent.is_active.is_(True))
    if status_filter:
        base = base.where(Agent.status == status_filter)
    if search:
        like = f"%{search}%"
        base = base.where(Agent.hostname.ilike(like))
    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    rows = (await db.execute(
        base.order_by(desc(Agent.last_heartbeat_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    dispatcher = get_dispatcher()
    items = [
        AgentListItem(
            id=a.id,
            fingerprint=a.fingerprint,
            hostname=a.hostname,
            os_name=a.os_name,
            os_version=a.os_version,
            agent_version=a.agent_version,
            status=a.status,
            last_heartbeat_at=a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
            registered_at=a.registered_at.isoformat() if a.registered_at else "",
            last_ip=a.last_ip,
            online_now=dispatcher.is_online(a.id),
        )
        for a in rows
    ]
    return AgentListResponse(items=items, total=int(total), page=page, page_size=page_size)


@router.get("/{agent_id}")
async def get_agent_detail(
    agent_id: str,
    _admin: str = Depends(require_agents_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    a = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    snapshot: dict[str, Any] | None = None
    if a.last_snapshot_json:
        try:
            snapshot = json.loads(a.last_snapshot_json)
        except Exception:
            snapshot = None
    return {
        "id": a.id,
        "fingerprint": a.fingerprint,
        "hostname": a.hostname,
        "os_name": a.os_name,
        "os_version": a.os_version,
        "agent_version": a.agent_version,
        "status": a.status,
        "registered_at": a.registered_at.isoformat() if a.registered_at else None,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
        "last_ip": a.last_ip,
        "online_now": get_dispatcher().is_online(a.id),
        "snapshot": snapshot,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin — commands
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/{agent_id}/command")
async def queue_command(
    agent_id: str,
    body: CommandRequestBody,
    admin_id: str = Depends(require_agents_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if not agent or not agent.is_active:
        raise HTTPException(status_code=404, detail="agent not found")
    row = await get_dispatcher().dispatch(
        db,
        agent_id=agent_id,
        command=body.command,
        args=body.args,
        timeout_secs=body.timeout_secs,
        issued_by=admin_id,
    )
    return {
        "command_id": row.id,
        "status": row.status,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
    }


@router.get("/{agent_id}/commands")
async def list_commands(
    agent_id: str,
    _admin: str = Depends(require_agents_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(AgentCommand)
        .where(AgentCommand.agent_id == agent_id)
        .order_by(desc(AgentCommand.issued_at))
        .limit(limit)
    )).scalars().all()
    return [
        {
            "id": r.id,
            "command": r.command,
            "args": json.loads(r.args_json) if r.args_json else [],
            "status": r.status,
            "exit_code": r.exit_code,
            "stdout": (r.stdout or "")[:32 * 1024],
            "stderr": (r.stderr or "")[:32 * 1024],
            "issued_by": r.issued_by,
            "issued_at": r.issued_at.isoformat() if r.issued_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "duration_ms": r.duration_ms,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# File transfer
# ─────────────────────────────────────────────────────────────────────────────


def _agent_files_root() -> Path:
    base = Path(os.environ.get("HELEN_AGENT_FILES_ROOT") or "./data/agent-files")
    base.mkdir(parents=True, exist_ok=True)
    return base


_DOWNLOAD_TOKENS: dict[str, tuple[Path, float, Optional[str]]] = {}
_DOWNLOAD_TOKEN_TTL = 600


def _purge_download_tokens() -> None:
    now = time.time()
    expired = [k for k, (_, ts, _) in _DOWNLOAD_TOKENS.items() if now - ts > _DOWNLOAD_TOKEN_TTL]
    for k in expired:
        _DOWNLOAD_TOKENS.pop(k, None)


@router.post("/{agent_id}/files/upload")
async def agent_upload(
    agent_id: str,
    file: UploadFile = File(...),
    sha256: str = Form(...),
    size: int = Form(...),
    session: dict[str, Any] = Depends(require_agent_session),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if session.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="token / path mismatch")
    root = _agent_files_root() / agent_id / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    file_id = secrets.token_urlsafe(16)
    dest = root / f"{file_id}-{Path(file.filename or 'blob.bin').name}"

    hasher = hashlib.sha256()
    written = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            out.write(chunk)
            written += len(chunk)
    actual = hasher.hexdigest()
    if actual != sha256.lower():
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="sha256 mismatch")
    if written != size:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="size mismatch")
    db.add(AgentEvent(
        agent_id=agent_id,
        event_type="file_uploaded",
        payload_json=json.dumps({"file_id": file_id, "size": written, "sha256": actual}),
    ))
    await db.commit()
    return {"file_id": file_id, "sha256": actual, "bytes": written}


@router.get("/{agent_id}/files/download/{token}")
async def agent_download(
    agent_id: str,
    token: str,
    session: dict[str, Any] = Depends(require_agent_session),
) -> FileResponse:
    if session.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="token / path mismatch")
    _purge_download_tokens()
    entry = _DOWNLOAD_TOKENS.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="invalid or expired download token")
    path, _, _ = entry
    if not path.exists():
        raise HTTPException(status_code=404, detail="file no longer present")
    return FileResponse(path, filename=path.name)


# Admin helper to mint a download token for the agent to consume.

class IssueDownloadRequest(BaseModel):
    server_path: str = Field(min_length=1, max_length=4096)
    expected_sha256: Optional[str] = None


@router.post("/{agent_id}/files/issue-download")
async def issue_download_token(
    agent_id: str,
    body: IssueDownloadRequest,
    _admin: str = Depends(require_agents_admin),
) -> dict[str, Any]:
    root = _agent_files_root().resolve()
    target = (root / body.server_path).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(status_code=400, detail="path escapes file root")
    if not target.exists():
        raise HTTPException(status_code=404, detail="server file not found")
    token = secrets.token_urlsafe(24)
    _DOWNLOAD_TOKENS[token] = (target, time.time(), body.expected_sha256)
    return {"token": token, "expires_in": _DOWNLOAD_TOKEN_TTL}


# ─────────────────────────────────────────────────────────────────────────────
# Update manifest & binary
# ─────────────────────────────────────────────────────────────────────────────


def _updates_root() -> Path:
    p = Path(os.environ.get("HELEN_AGENT_UPDATES_DIR") or "./data/agent-updates")
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("/update/manifest")
async def update_manifest(
    _session: dict[str, Any] = Depends(require_agent_session),
) -> dict[str, Any]:
    p = _updates_root() / "latest.json"
    if not p.exists():
        # Empty / placeholder manifest — agent treats as "no update".
        return {"version": "0.0.0", "url": "", "sha256": "0" * 64}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="bad manifest")


@router.get("/update/binary")
async def update_binary(
    _session: dict[str, Any] = Depends(require_agent_session),
) -> FileResponse:
    p = _updates_root() / "helen-agent.exe"
    if not p.exists():
        raise HTTPException(status_code=404, detail="no published binary")
    return FileResponse(p, filename="helen-agent.exe", media_type="application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# Revocation
# ─────────────────────────────────────────────────────────────────────────────


@router.delete("/{agent_id}")
async def revoke_agent(
    agent_id: str,
    admin_id: str = Depends(require_agents_admin),
    db: AsyncSession = Depends(get_db),
    reason: str = Query(default="admin-revoke", max_length=256),
) -> dict[str, str]:
    a = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    await get_agent_manager().revoke(db, agent_id, reason)
    # Force-close any live control connection.
    conn = get_dispatcher().get(agent_id)
    if conn:
        try:
            await conn.websocket.close()
        except Exception:
            pass
    logger.info("agent_revoked_by_admin", agent_id=agent_id, admin_id=admin_id)
    return {"status": "revoked"}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — agent control channel
# ─────────────────────────────────────────────────────────────────────────────


async def _authenticate_ws(websocket: WebSocket) -> Optional[dict[str, Any]]:
    """Pull the bearer token from the standard `Authorization` header.

    Falls back to the `?access_token=` query string for clients that cannot
    set headers (browsers in particular).
    """
    auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    token: Optional[str] = None
    if auth and auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
    if not token:
        token = websocket.query_params.get("access_token")
    if not token:
        return None
    try:
        return decode_token(token)
    except HTTPException:
        return None


@router.websocket("/{agent_id}/control")
async def agent_control_channel(websocket: WebSocket, agent_id: str) -> None:
    payload = await _authenticate_ws(websocket)
    if not payload or payload.get("agent_id") != agent_id:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    dispatcher = get_dispatcher()
    conn = await dispatcher.attach(agent_id, websocket)

    # Persist online state.
    async with async_session_factory() as db:
        a = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if a:
            a.status = "online"
            a.last_heartbeat_at = datetime.now(timezone.utc)
            db.add(AgentEvent(agent_id=agent_id, event_type="online", payload_json=None))
            await db.commit()

    try:
        while True:
            text = await websocket.receive_text()
            try:
                frame = json.loads(text)
            except Exception:
                continue
            kind = frame.get("type")
            if kind == "pong":
                continue
            if kind == "agent_info":
                logger.info("agent_info", agent_id=agent_id, version=frame.get("version"))
                continue
            if kind == "command_stream":
                inner = frame.get("payload") or frame
                await dispatcher.fan_out_event(agent_id, {
                    "type": "command_stream",
                    "stream": inner,
                })
                continue
            # Detect a CommandStreamEvent::Finished payload from the Rust agent.
            inner_type = frame.get("type")
            if inner_type == "finished" or "exit_code" in frame:
                cmd_id = frame.get("command_id")
                if cmd_id:
                    await dispatcher.handle_command_result(
                        agent_id=agent_id,
                        command_id=cmd_id,
                        exit_code=int(frame.get("exit_code", -1)),
                        stdout=str(frame.get("stdout", "")),
                        stderr=str(frame.get("stderr", "")),
                        duration_ms=int(frame.get("duration_ms", 0)),
                        timed_out=bool(frame.get("timed_out", False)),
                    )
                continue
            if inner_type in {"stdout", "stderr"}:
                await dispatcher.fan_out_event(agent_id, frame)
                continue
            if inner_type in {
                "upload_complete",
                "upload_failed",
                "download_complete",
                "download_failed",
                "screen_chunk",
                "screen_finished",
                "error",
            }:
                await dispatcher.fan_out_event(agent_id, frame)
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("agent_control_ws_error", agent_id=agent_id)
    finally:
        await dispatcher.detach(agent_id, websocket)
        async with async_session_factory() as db:
            a = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
            if a:
                a.status = "offline"
                db.add(AgentEvent(agent_id=agent_id, event_type="offline", payload_json=None))
                await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — admin event feed
# ─────────────────────────────────────────────────────────────────────────────


@router.websocket("/{agent_id}/events")
async def admin_event_feed(websocket: WebSocket, agent_id: str) -> None:
    payload = await _authenticate_ws(websocket)
    if not payload:
        await websocket.close(code=1008)
        return
    role = (payload.get("role") or "").lower()
    if role not in {"admin", "superadmin"}:
        # Verify via RBAC enforcer
        try:
            from app.services.rbac.enforcer import user_has_permission
            async with async_session_factory() as db:
                user_id = payload.get("sub", "")
                ok = await user_has_permission(db, user_id, "agents.manage")
        except Exception:
            ok = False
        if not ok:
            await websocket.close(code=1008)
            return

    await websocket.accept()
    dispatcher = get_dispatcher()
    await dispatcher.subscribe_events(agent_id, websocket)
    try:
        while True:
            # Keep the connection alive; client may send pings.
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await dispatcher.unsubscribe_events(agent_id, websocket)


__all__ = ["router"]
