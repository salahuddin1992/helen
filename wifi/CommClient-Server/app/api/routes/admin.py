"""
Admin REST API endpoints — server statistics, user management, backups.

RBAC enforced: All endpoints require at minimum "admin" role unless noted.
The role claim is verified from the JWT via `require_role("admin")` dependency.

Endpoints:
  GET    /api/admin/stats                  — Server statistics dashboard
  GET    /api/admin/active-calls           — List all active calls
  POST   /api/admin/kick/{user_id}         — Force disconnect user
  POST   /api/admin/ban/{user_id}          — Ban user (soft: is_active=False)
  POST   /api/admin/unban/{user_id}        — Unban user
  POST   /api/admin/set-role/{user_id}     — Change user role (admin only)
  POST   /api/admin/cleanup/sessions       — Clean expired JWT sessions
  POST   /api/admin/cleanup/files          — Clean orphaned files
  GET    /api/admin/backups                — List all backups
  POST   /api/admin/backups                — Create a new backup
  POST   /api/admin/backups/{name}/restore — Restore a backup
  DELETE /api/admin/backups/{name}         — Delete a backup
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security_utils import require_role, VALID_ROLES
from app.models.user import User
from app.services.admin_service import admin_service
from app.services.backup_service import backup_service
from app.services.server_config_service import server_config_service

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Pydantic models ───────────────────────────────────────

class SetRoleRequest(BaseModel):
    role: str


class ServerConfigUpdate(BaseModel):
    server_name: str


# ────────────────────────────────────────────────────────────────
# Server Statistics & Health
# ────────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_server_stats(
    user_id: str = Depends(require_role("admin")),
):
    """
    Return comprehensive server statistics: uptime, user counts, resource usage,
    database size, active calls, and system metrics.
    """
    try:
        stats = await admin_service.get_server_stats()
        audit_log("admin.stats_requested", user_id=user_id, success=True)
        return stats
    except Exception as e:
        logger.error("get_server_stats_error", error=str(e), user_id=user_id)
        audit_log("admin.stats_requested", user_id=user_id, success=False, details={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve server statistics",
        )


@router.get("/active-calls")
async def get_active_calls(
    user_id: str = Depends(require_role("admin")),
):
    """
    Return list of currently active calls with participant information.
    """
    try:
        calls = await admin_service.get_active_calls()
        audit_log("admin.active_calls_requested", user_id=user_id, success=True)
        return {"calls": calls, "count": len(calls)}
    except Exception as e:
        logger.error("get_active_calls_error", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve active calls",
        )


@router.get("/connectivity")
async def get_connectivity_status(
    user_id: str = Depends(require_role("admin")),
):
    """Aggregate view of the ConnectivityOrchestrator. Reports which
    strategies (LAN / UPnP / reverse-tunnel / hole-punch / relay) are
    *configured* and which are *actively serving traffic* right now.
    Safe to poll from the admin dashboard every few seconds.
    """
    from app.services.connectivity import orchestrator as _conn
    return _conn.status()


class TunnelConfigRequest(BaseModel):
    ws_url: str
    token: str
    display_name: str | None = None


@router.post("/connectivity/tunnel")
async def configure_tunnel(
    body: TunnelConfigRequest,
    user_id: str = Depends(require_role("admin")),
):
    """Replace the running reverse-tunnel with one pointed at the given
    rendezvous. The token never leaves the server — the response echoes
    only the sanitized ``status()`` payload."""
    from app.services.connectivity import orchestrator as _conn
    try:
        result = await _conn.configure_tunnel(
            ws_url=body.ws_url, token=body.token,
            display_name=body.display_name,
        )
        audit_log("admin.connectivity_tunnel_configured", user_id=user_id, success=True)
        return result
    except Exception as e:
        logger.error("connectivity_tunnel_configure_failed", error=str(e))
        audit_log("admin.connectivity_tunnel_configured", user_id=user_id,
                  success=False, details={"error": str(e)})
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/connectivity/tunnel")
async def disable_tunnel(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.connectivity import orchestrator as _conn
    result = await _conn.disable_tunnel()
    audit_log("admin.connectivity_tunnel_disabled", user_id=user_id, success=True)
    return result


class CameraSourceCreate(BaseModel):
    name: str
    url: str
    type: str = "mjpeg"
    note: str = ""


@router.get("/camera-sources")
async def list_camera_sources(
    user_id: str = Depends(require_role("admin")),
):
    """Return all external camera sources (IP cams, MJPEG/HLS/WHIP URLs)
    registered by operators. This is the list the client's camera picker
    merges with OS webcams and paired phone sources."""
    from app.services.camera_sources import camera_sources
    return {"sources": camera_sources.list_all()}


@router.post("/camera-sources")
async def create_camera_source(
    body: CameraSourceCreate,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.camera_sources import camera_sources
    try:
        entry = camera_sources.add(
            name=body.name, url=body.url, type_=body.type,
            added_by=user_id, note=body.note,
        )
        audit_log("admin.camera_source_added", user_id=user_id, success=True,
                  details={"id": entry["id"], "url": entry["url"]})
        return entry
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/camera-sources/{camera_id}")
async def delete_camera_source(
    camera_id: str,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.camera_sources import camera_sources
    ok = camera_sources.remove(camera_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    audit_log("admin.camera_source_removed", user_id=user_id, success=True,
              details={"id": camera_id})
    return {"ok": True}


@router.post("/camera-sources/{camera_id}/test")
async def test_camera_source(
    camera_id: str,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.camera_sources import camera_sources
    result = camera_sources.test_url(camera_id)
    audit_log("admin.camera_source_tested", user_id=user_id,
              success=bool(result.get("ok")), details={"id": camera_id})
    return result


@router.get("/diagnostics/network")
async def admin_network_diagnostics(
    user_id: str = Depends(require_role("admin")),
):
    """Run the full router/firewall/AP-isolation diagnostic sweep and
    return a table the admin UI renders as one row per check. Takes
    ~3-5 seconds depending on how many peers are already known."""
    import os as _os_diag
    from app.services.network_diagnostics import run_diagnostics
    try:
        port = int(_os_diag.environ.get("PORT", "3000"))
    except ValueError:
        port = 3000
    return await run_diagnostics(port)


@router.get("/federation/bridges")
async def list_federation_bridges(
    user_id: str = Depends(require_role("admin")),
):
    """Per-peer bridge view: counters + peer registry info merged.

    Admin dashboard renders one row per known peer showing emits
    sent/received, forwards attempted, dedup drops, bytes in/out, and
    idle time. Used to diagnose whether a chain-routing hop is live
    and how much traffic it's carrying.
    """
    from app.services.federation_metrics import per_peer_snapshot
    from app.services.peer_registry import peer_registry
    peers = await peer_registry.list(include_stale=True)
    peer_map = {p.server_id: p for p in peers}
    rows = []
    for row in per_peer_snapshot():
        pid = row.get("server_id", "")
        pr = peer_map.get(pid)
        merged = dict(row)
        if pr is not None:
            merged.update({
                "name": pr.name,
                "host": pr.host,
                "port": pr.port,
                "version": pr.version,
                "is_stale": pr.is_stale,
                "age_seconds": round(pr.age_seconds, 1),
            })
        rows.append(merged)
    return {"bridges": rows, "count": len(rows)}


@router.get("/federation/metrics")
async def get_federation_metrics(
    user_id: str = Depends(require_role("admin")),
):
    """Global federation counters: HMAC, relay, breakers, presence + the
    per-peer snapshot + a rolling 50-event log. One-stop /metrics for
    the admin 'Federation Bridges' panel."""
    from app.services.federation_metrics import snapshot
    return snapshot()


@router.get("/federation/events")
async def recent_federation_events(
    limit: int = 100,
    user_id: str = Depends(require_role("admin")),
):
    """Recent bridge events (forwards, dedup drops, local deliveries).
    Useful for pull-based dashboards that don't use the Socket.IO live
    stream."""
    from app.services.federation_metrics import recent_events
    return {"events": recent_events(min(max(limit, 1), 200))}


@router.get("/connected-clients")
async def get_connected_clients(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the live roster of socket connections — one entry per socket
    (not per user), so users with multiple tabs/devices show multiple rows.

    Each entry carries the session metadata we captured at connect time
    (``remote_addr``, ``device_type``) plus the user's profile fields
    resolved from the DB on demand. Use this to prove an operator's
    client actually landed on this server.
    """
    from app.socket.server import sio
    from app.services.presence_service import presence_service

    # Snapshot the presence map so iteration is safe while sockets churn.
    sid_to_user = dict(presence_service._sid_user)  # shallow copy of internal map
    if not sid_to_user:
        audit_log("admin.connected_clients_requested", user_id=user_id,
                  success=True, details={"count": 0})
        return {"clients": [], "count": 0}

    user_ids = list(set(sid_to_user.values()))
    user_rows = (
        await db.execute(select(User).where(User.id.in_(user_ids)))
    ).scalars().all()
    user_map = {u.id: u for u in user_rows}

    clients: list[dict] = []
    for sid, uid in sid_to_user.items():
        try:
            session = await sio.get_session(sid)
        except Exception:
            session = {}
        user = user_map.get(uid)
        clients.append({
            "sid": sid,
            "user_id": uid,
            "username": getattr(user, "username", None),
            "display_name": getattr(user, "display_name", None),
            "role": getattr(user, "role", None),
            "remote_addr": session.get("remote_addr") if isinstance(session, dict) else None,
            "device_type": session.get("device_type") if isinstance(session, dict) else None,
            "user_agent": session.get("user_agent") if isinstance(session, dict) else None,
            "status": await presence_service.get_status(uid),
            "connected_at": session.get("connected_at") if isinstance(session, dict) else None,
        })

    # Stable order: username asc, then sid.
    clients.sort(key=lambda c: ((c.get("username") or "").lower(), c["sid"]))
    audit_log("admin.connected_clients_requested", user_id=user_id,
              success=True, details={"count": len(clients)})
    return {"clients": clients, "count": len(clients)}


# ────────────────────────────────────────────────────────────────
# User Management
# ────────────────────────────────────────────────────────────────


@router.post("/kick/{target_user_id}")
async def kick_user(
    target_user_id: str,
    user_id: str = Depends(require_role("admin")),
):
    """
    Force disconnect all socket connections for a user.
    This does not ban the user — they can reconnect.
    """
    try:
        if target_user_id == user_id:
            audit_log(
                "admin.kick_user",
                user_id=user_id,
                success=False,
                details={"target_user_id": target_user_id, "reason": "cannot_kick_self"},
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot kick yourself",
            )

        success = await admin_service.kick_user(target_user_id)
        audit_log(
            "admin.kick_user",
            user_id=user_id,
            success=success,
            details={"target_user_id": target_user_id},
        )

        if success:
            return {"status": "kicked", "user_id": target_user_id}
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} is not connected",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("kick_user_error", error=str(e), user_id=user_id, target_user_id=target_user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to kick user",
        )


@router.post("/ban/{target_user_id}")
async def ban_user(
    target_user_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Ban a user (soft ban: set is_active=False).
    Prevents login but does not delete the user or their data.
    Also kicks the user if currently connected.
    """
    try:
        if target_user_id == user_id:
            audit_log(
                "admin.ban_user",
                user_id=user_id,
                success=False,
                details={"target_user_id": target_user_id, "reason": "cannot_ban_self"},
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot ban yourself",
            )

        success = await admin_service.ban_user(db, target_user_id)
        audit_log(
            "admin.ban_user",
            user_id=user_id,
            success=success,
            details={"target_user_id": target_user_id},
        )

        if success:
            return {"status": "banned", "user_id": target_user_id}
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("ban_user_error", error=str(e), user_id=user_id, target_user_id=target_user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ban user",
        )


@router.post("/unban/{target_user_id}")
async def unban_user(
    target_user_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Unban a user (soft unban: set is_active=True).
    Allows the user to login again.
    """
    try:
        success = await admin_service.unban_user(db, target_user_id)
        audit_log(
            "admin.unban_user",
            user_id=user_id,
            success=success,
            details={"target_user_id": target_user_id},
        )

        if success:
            return {"status": "unbanned", "user_id": target_user_id}
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("unban_user_error", error=str(e), user_id=user_id, target_user_id=target_user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unban user",
        )


# ────────────────────────────────────────────────────────────────
# Role Management
# ────────────────────────────────────────────────────────────────


class AdminResetPasswordBody(BaseModel):
    new_password: str


@router.post("/reset-password/{target_user_id}", status_code=204)
async def admin_reset_password(
    target_user_id: str,
    body: AdminResetPasswordBody,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Admin sets a user's password without knowing the old one.

    Use cases: a user forgot their password, an operator needs to lock
    out a compromised account by rotating its credential, or onboarding
    a freshly-imported account that has no usable password.

    Always logged to the audit trail (target_user_id, actor user_id).
    """
    from app.core.security import hash_password_async
    from app.core.crypto import validate_password_strength

    if len(body.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters",
        )

    target = (
        await db.execute(select(User).where(User.id == target_user_id))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    ok, reason = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    target.password_hash = await hash_password_async(body.new_password)
    await db.commit()

    audit_log(
        "admin.reset_password",
        user_id=user_id,
        success=True,
        details={"target_user_id": target_user_id, "target_username": target.username},
    )
    return Response(status_code=204)


@router.post("/set-role/{target_user_id}")
async def set_user_role(
    target_user_id: str,
    body: SetRoleRequest,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Change a user's role. Admin only.
    Valid roles: "user", "moderator", "admin".
    Cannot demote yourself (prevents lockout).
    """
    try:
        if body.role not in VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}",
            )

        if target_user_id == user_id:
            audit_log(
                "admin.set_role",
                user_id=user_id,
                success=False,
                details={"target_user_id": target_user_id, "reason": "cannot_change_own_role"},
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change your own role (prevents lockout)",
            )

        result = await db.execute(select(User).where(User.id == target_user_id))
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} not found",
            )

        old_role = target.role
        target.role = body.role
        await db.commit()

        audit_log(
            "admin.set_role",
            user_id=user_id,
            success=True,
            details={
                "target_user_id": target_user_id,
                "old_role": old_role,
                "new_role": body.role,
            },
        )

        logger.info(
            "user_role_changed",
            admin_id=user_id,
            target_id=target_user_id,
            old_role=old_role,
            new_role=body.role,
        )

        return {
            "status": "role_updated",
            "user_id": target_user_id,
            "old_role": old_role,
            "new_role": body.role,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("set_role_error", error=str(e), user_id=user_id, target_user_id=target_user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set user role",
        )


# ────────────────────────────────────────────────────────────────
# Maintenance & Cleanup
# ────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────
# Per-user session management (admin — force-logout a specific user)
# ────────────────────────────────────────────────────────────────


@router.get("/users/{target_user_id}/sessions")
async def admin_list_user_sessions(
    target_user_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List every active session for the target user — for audit / forensics."""
    from app.services.session_service import SessionService

    sessions = await SessionService.list_sessions(db, target_user_id)
    return {
        "user_id": target_user_id,
        "total": len(sessions),
        "sessions": [
            {
                "id": s.id,
                "device_name": s.device_name,
                "ip_address": s.ip_address,
                "user_agent": s.user_agent,
                "last_activity": s.last_activity.isoformat() if s.last_activity else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            }
            for s in sessions
        ],
    }


@router.delete("/users/{target_user_id}/sessions/{session_id}", status_code=204)
async def admin_revoke_user_session(
    target_user_id: str,
    session_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Force-revoke a single session row — the device must re-authenticate."""
    from app.services.session_service import SessionService

    try:
        await SessionService.admin_revoke_session(db, target_user_id, session_id)
        audit_log(
            "admin.session_revoked",
            user_id=user_id,
            success=True,
            details={"target_user_id": target_user_id, "session_id": session_id},
        )
        return Response(status_code=204)
    except Exception as e:
        logger.error(
            "admin_session_revoke_error",
            error=str(e), user_id=user_id, target_user_id=target_user_id,
        )
        audit_log(
            "admin.session_revoked",
            user_id=user_id, success=False,
            details={"target_user_id": target_user_id, "session_id": session_id, "error": str(e)},
        )
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/users/{target_user_id}/sessions/revoke-all")
async def admin_revoke_all_user_sessions(
    target_user_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Force-logout every device for this user. Used for account takeover response."""
    from app.services.session_service import SessionService

    try:
        count = await SessionService.admin_revoke_all_for_user(db, target_user_id)
        audit_log(
            "admin.sessions_revoked_all",
            user_id=user_id, success=True,
            details={"target_user_id": target_user_id, "revoked": count},
        )
        return {"target_user_id": target_user_id, "revoked": count}
    except Exception as e:
        logger.error(
            "admin_sessions_revoke_all_error",
            error=str(e), user_id=user_id, target_user_id=target_user_id,
        )
        audit_log(
            "admin.sessions_revoked_all",
            user_id=user_id, success=False,
            details={"target_user_id": target_user_id, "error": str(e)},
        )
        raise HTTPException(status_code=500, detail="Failed to revoke sessions")


# ────────────────────────────────────────────────────────────────
# Expired-session cleanup (bulk)
# ────────────────────────────────────────────────────────────────


@router.post("/cleanup/sessions")
async def cleanup_expired_sessions(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete all expired JWT sessions from the database.
    Returns count of sessions deleted.
    """
    try:
        deleted_count = await admin_service.cleanup_expired_sessions(db)
        audit_log(
            "admin.cleanup_sessions",
            user_id=user_id,
            success=True,
            details={"deleted_count": deleted_count},
        )
        return {"status": "cleanup_completed", "deleted_count": deleted_count}

    except Exception as e:
        logger.error("cleanup_sessions_error", error=str(e), user_id=user_id)
        audit_log("admin.cleanup_sessions", user_id=user_id, success=False, details={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cleanup sessions",
        )


@router.post("/cleanup/files")
async def cleanup_orphaned_files(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete file records that are not referenced by any message.
    Returns count of files deleted.
    """
    try:
        deleted_count = await admin_service.cleanup_orphaned_files(db)
        audit_log(
            "admin.cleanup_files",
            user_id=user_id,
            success=True,
            details={"deleted_count": deleted_count},
        )
        return {"status": "cleanup_completed", "deleted_count": deleted_count}

    except Exception as e:
        logger.error("cleanup_files_error", error=str(e), user_id=user_id)
        audit_log("admin.cleanup_files", user_id=user_id, success=False, details={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cleanup files",
        )


# ────────────────────────────────────────────────────────────────
# Automated backup scheduler — observability + manual trigger
# ────────────────────────────────────────────────────────────────


@router.get("/backups/scheduler")
async def get_backup_scheduler_state(
    user_id: str = Depends(require_role("admin")),
):
    """Return the auto-backup scheduler's live state: last run, counts, config."""
    from app.services import backup_scheduler
    return backup_scheduler.get_state().snapshot()


@router.post("/backups/run-now")
async def trigger_backup_now(
    user_id: str = Depends(require_role("admin")),
):
    """Force an immediate snapshot — does not alter the periodic schedule."""
    from app.services import backup_scheduler
    try:
        res = await backup_scheduler.trigger_now()
        audit_log(
            "admin.backup_triggered",
            user_id=user_id,
            success=res.ok,
            details={"backup_name": res.backup_name, "error": res.error},
        )
        return {
            "ok": res.ok,
            "backup_name": res.backup_name,
            "pruned": res.pruned,
            "error": res.error,
            "ts": res.ts.isoformat(),
        }
    except Exception as e:
        logger.error("backup_trigger_failed", error=str(e), user_id=user_id)
        audit_log(
            "admin.backup_triggered",
            user_id=user_id, success=False, details={"error": str(e)},
        )
        raise HTTPException(status_code=500, detail="Backup trigger failed")


# ────────────────────────────────────────────────────────────────
# Backup & Restore
# ────────────────────────────────────────────────────────────────


@router.get("/backups")
async def list_backups(
    user_id: str = Depends(require_role("admin")),
):
    """
    List all database backups with metadata (name, size, creation time).
    """
    try:
        backups = await backup_service.list_backups()
        audit_log("admin.backups_listed", user_id=user_id, success=True, details={"count": len(backups)})
        return {"backups": backups, "count": len(backups)}

    except Exception as e:
        logger.error("list_backups_error", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list backups",
        )


@router.post("/backups")
async def create_backup(
    user_id: str = Depends(require_role("admin")),
):
    """
    Create a new timestamped backup of the database.
    Returns backup filename and metadata.
    """
    try:
        backup_name = await backup_service.create_backup()
        backups = await backup_service.list_backups()
        backup_info = next((b for b in backups if b["name"] == backup_name), None)

        audit_log(
            "admin.backup_created",
            user_id=user_id,
            success=True,
            details={"backup_name": backup_name, "size_bytes": backup_info.get("size_bytes", 0) if backup_info else 0},
        )

        return {
            "status": "backup_created",
            "backup_name": backup_name,
            "metadata": backup_info,
        }

    except FileNotFoundError as e:
        audit_log("admin.backup_created", user_id=user_id, success=False, details={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database file not found",
        )
    except Exception as e:
        logger.error("create_backup_error", error=str(e), user_id=user_id)
        audit_log("admin.backup_created", user_id=user_id, success=False, details={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create backup",
        )


@router.post("/backups/{backup_name}/restore")
async def restore_backup(
    backup_name: str,
    user_id: str = Depends(require_role("admin")),
):
    """
    Restore a specific backup — DANGEROUS operation that overwrites the active database.
    Creates a protective backup before restoring.
    """
    try:
        if not backup_name or ".." in backup_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid backup name",
            )

        success = await backup_service.restore_backup(backup_name)

        if success:
            audit_log(
                "admin.backup_restored",
                user_id=user_id,
                success=True,
                details={"backup_name": backup_name},
            )
            return {"status": "backup_restored", "backup_name": backup_name}
        else:
            audit_log(
                "admin.backup_restored",
                user_id=user_id,
                success=False,
                details={"backup_name": backup_name, "reason": "restore_failed"},
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Backup '{backup_name}' not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("restore_backup_error", error=str(e), user_id=user_id, backup_name=backup_name)
        audit_log(
            "admin.backup_restored",
            user_id=user_id,
            success=False,
            details={"backup_name": backup_name, "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restore backup",
        )


@router.post("/backups/{backup_name}/verify")
async def verify_backup(
    backup_name: str,
    user_id: str = Depends(require_role("admin")),
):
    """
    Run SQLite integrity checks on a stored backup without touching the
    live DB. Returns the structured verification report so the operator
    can decide whether to trust this snapshot for restore.
    """
    if not backup_name or ".." in backup_name or "/" in backup_name or "\\" in backup_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup name")
    if not backup_name.startswith("commclient_backup_") or not backup_name.endswith(".db"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup name")

    try:
        report = await backup_service.verify_backup(backup_name)
    except Exception as e:
        logger.error("backup_verify_exception", backup_name=backup_name, error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="verification failed")

    audit_log(
        "admin.backup_verified",
        user_id=user_id,
        success=bool(report.get("ok")),
        details={
            "backup_name": backup_name,
            "integrity_ok": report.get("integrity_ok"),
            "quick_ok": report.get("quick_ok"),
            "schema_ok": report.get("schema_ok"),
            "error": report.get("error"),
        },
    )
    return {"backup_name": backup_name, **report}


@router.get("/backups/{backup_name}/download")
async def download_backup(
    backup_name: str,
    user_id: str = Depends(require_role("admin")),
):
    """
    Stream a backup .db file back to the caller. Intended for operators
    who want a copy off-host; pairs with the "Export DB" button in the
    admin dashboard.
    """
    # Same filename guardrails as delete/restore — no traversal, must
    # match the backup_service naming scheme.
    if not backup_name or ".." in backup_name or "/" in backup_name or "\\" in backup_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup name")
    if not backup_name.startswith("commclient_backup_") or not backup_name.endswith(".db"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup name")

    # Resolve against the backup service's own directory rather than
    # re-deriving the path — keeps one source of truth.
    backup_path = backup_service._backup_dir / backup_name
    if not backup_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Backup '{backup_name}' not found")

    audit_log(
        "admin.backup_downloaded",
        user_id=user_id,
        success=True,
        details={"backup_name": backup_name, "size_bytes": backup_path.stat().st_size},
    )
    return FileResponse(
        path=str(backup_path),
        media_type="application/octet-stream",
        filename=backup_name,
    )


@router.delete("/backups/{backup_name}")
async def delete_backup(
    backup_name: str,
    user_id: str = Depends(require_role("admin")),
):
    """
    Delete a specific backup file.
    """
    try:
        if not backup_name or ".." in backup_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid backup name",
            )

        success = await backup_service.delete_backup(backup_name)

        if success:
            audit_log(
                "admin.backup_deleted",
                user_id=user_id,
                success=True,
                details={"backup_name": backup_name},
            )
            return {"status": "backup_deleted", "backup_name": backup_name}
        else:
            audit_log(
                "admin.backup_deleted",
                user_id=user_id,
                success=False,
                details={"backup_name": backup_name, "reason": "backup_not_found"},
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Backup '{backup_name}' not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_backup_error", error=str(e), user_id=user_id, backup_name=backup_name)
        audit_log(
            "admin.backup_deleted",
            user_id=user_id,
            success=False,
            details={"backup_name": backup_name, "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete backup",
        )


# ────────────────────────────────────────────────────────────────
# Server Configuration
# ────────────────────────────────────────────────────────────────


@router.get("/server-config")
async def get_server_config(
    user_id: str = Depends(require_role("admin")),
):
    """Return runtime-editable server settings (e.g. server_name)."""
    snap = server_config_service.snapshot()
    return {"server_name": snap.get("SERVER_NAME")}


@router.patch("/server-config")
async def update_server_config(
    body: ServerConfigUpdate,
    user_id: str = Depends(require_role("admin")),
):
    """Persist a new server name and apply it live (broadcasts + /health)."""
    try:
        snap = server_config_service.update_server_name(body.server_name)
    except ValueError as exc:
        audit_log(
            "admin.server_config_updated",
            user_id=user_id,
            success=False,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        logger.error("update_server_config_error", error=str(exc), user_id=user_id)
        audit_log(
            "admin.server_config_updated",
            user_id=user_id,
            success=False,
            details={"error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update server config",
        )

    audit_log(
        "admin.server_config_updated",
        user_id=user_id,
        success=True,
        details={"server_name": snap.get("SERVER_NAME")},
    )
    return {"server_name": snap.get("SERVER_NAME")}


# ────────────────────────────────────────────────────────────────
# Server Roles — operator-toggled role composition
# ────────────────────────────────────────────────────────────────
#
# Each Helen-Server instance can run in different role configurations.
# Some roles are structural and always on (auth, signaling, messaging,
# presence, database, admin). Others are toggleable: SFU, relay,
# recording, file transfer, metrics, auto-degrade. The current mode
# and strategy caps are also exposed so a phone operator can force
# audio-only / chat-only under stress without SSH'ing the box.
#
# Persisted to data/server_roles.json; loaded at startup. Changes take
# effect immediately for new sessions; active calls finish under the
# old config (safer than mid-call mode flips).

import json as _roles_json
from pathlib import Path as _RolesPath

_ROLES_DEFAULT = {
    # Structural — cannot be disabled from the panel.
    "auth":       {"enabled": True, "locked": True,
                   "desc": "Identity, JWT issuance, session registry"},
    "signaling":  {"enabled": True, "locked": True,
                   "desc": "WebSocket negotiation, SDP/ICE routing"},
    "messaging":  {"enabled": True, "locked": True,
                   "desc": "Text delivery, offline queue, ack/read"},
    "presence":   {"enabled": True, "locked": True,
                   "desc": "Online/typing/last-seen heartbeats"},
    "database":   {"enabled": True, "locked": True,
                   "desc": "Persistent store (users, channels, messages)"},
    "admin":      {"enabled": True, "locked": True,
                   "desc": "Operator REST + WS surface"},
    # Optional — toggleable.
    "sfu":        {"enabled": True, "locked": False,
                   "desc": "Selective Forwarding Unit (group video)"},
    "relay":      {"enabled": True, "locked": False,
                   "desc": "TURN-like blind relay for NAT fallback"},
    "recording":  {"enabled": False, "locked": False,
                   "desc": "Disk-record SFU streams (high IO)"},
    "file_transfer": {"enabled": True, "locked": False,
                      "desc": "Chunked resumable upload/download"},
    "metrics":    {"enabled": True, "locked": False,
                   "desc": "Telemetry push + decision inputs"},
    "federation": {"enabled": False, "locked": False,
                   "desc": "Cross-server bridging"},
    "auto_degrade": {"enabled": True, "locked": False,
                     "desc": "Auto-downshift media under stress"},
    # Policy knobs (not strict roles, but live here for one UI).
    "policy_mode": {
        "value": "auto", "locked": False,
        "options": ["auto", "chat_only", "audio_only",
                    "video_ok", "no_sfu_p2p_only", "no_relay"],
        "desc": "Forced media strategy (overrides auto-degrade if not auto)",
    },
    "sfu_max_participants": {"value": 50, "locked": False, "min": 2, "max": 500,
                             "desc": "Hard cap before SFU refuses new publishers"},
    "cpu_downshift_pct":    {"value": 80, "locked": False, "min": 40, "max": 95,
                             "desc": "CPU% threshold that forces audio-only"},
    "loss_audio_pct":       {"value": 8,  "locked": False, "min": 1, "max": 50,
                             "desc": "Packet-loss%% threshold that forces audio-only"},
    "loss_chat_pct":        {"value": 15, "locked": False, "min": 1, "max": 80,
                             "desc": "Packet-loss%% threshold that forces chat-only"},
}

def _roles_path() -> "_RolesPath":
    # Anchor to the CommClient-Server/data/ dir regardless of run mode.
    data_dir = _RolesPath(__file__).resolve().parents[3] / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "server_roles.json"

def _load_roles() -> dict:
    try:
        p = _roles_path()
        if p.is_file():
            stored = _roles_json.loads(p.read_text(encoding="utf-8"))
            # Merge with defaults so new keys appear automatically.
            merged = _roles_json.loads(_roles_json.dumps(_ROLES_DEFAULT))
            for k, v in stored.items():
                if k in merged and isinstance(merged[k], dict):
                    merged[k].update(v if isinstance(v, dict) else {})
            return merged
    except Exception as e:
        logger.warning("server_roles_load_failed", error=str(e))
    return _roles_json.loads(_roles_json.dumps(_ROLES_DEFAULT))

def _save_roles(roles: dict) -> None:
    try:
        _roles_path().write_text(_roles_json.dumps(roles, indent=2),
                                 encoding="utf-8")
    except Exception as e:
        logger.warning("server_roles_save_failed", error=str(e))


@router.get("/server-roles")
async def get_server_roles(user_id: str = Depends(require_role("admin"))):
    """Return current server-role composition + policy knobs."""
    return {"roles": _load_roles()}


from typing import Any as _RolesAny  # local import — keeps top of file untouched


class _ServerRolesUpdate(BaseModel):
    updates: dict[str, _RolesAny]


@router.patch("/server-roles")
async def update_server_roles(
    body: _ServerRolesUpdate,
    user_id: str = Depends(require_role("admin")),
):
    """Apply a partial update to server roles.

    Body: {"updates": {"sfu": {"enabled": false},
                       "policy_mode": {"value": "audio_only"}}}
    Locked roles silently ignore enabled flips. Invalid policy_mode
    values return 400.
    """
    roles = _load_roles()
    applied = {}
    for key, patch in (body.updates or {}).items():
        if key not in roles:
            continue
        current = roles[key]
        if current.get("locked"):
            # Allow overriding "desc" etc. only, never enabled.
            continue
        if not isinstance(patch, dict):
            continue
        # Toggle boolean roles.
        if "enabled" in patch and isinstance(patch["enabled"], bool):
            current["enabled"] = patch["enabled"]
            applied[key] = {"enabled": patch["enabled"]}
        # Enum policy_mode.
        if "value" in patch:
            v = patch["value"]
            options = current.get("options")
            if options and v not in options:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid value for {key}: must be one of {options}",
                )
            # Numeric clamps.
            if current.get("min") is not None or current.get("max") is not None:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"{key} must be an integer",
                    )
                if current.get("min") is not None and v < current["min"]:
                    v = current["min"]
                if current.get("max") is not None and v > current["max"]:
                    v = current["max"]
            current["value"] = v
            applied[key] = {"value": v}
    _save_roles(roles)
    audit_log(
        "admin.server_roles_updated",
        user_id=user_id,
        success=True,
        details={"applied": applied},
    )
    return {"roles": roles, "applied": applied}


# ────────────────────────────────────────────────────────────────
# Control Plane — automatic decision engine
# ────────────────────────────────────────────────────────────────

from app.services.control_plane import ControlPlane as _ControlPlane


@router.get("/control-plane/status")
async def control_plane_status(user_id: str = Depends(require_role("admin"))):
    return _ControlPlane.instance().status()


@router.get("/control-plane/decisions")
async def control_plane_decisions(
    limit: int = 50,
    user_id: str = Depends(require_role("admin")),
):
    limit = max(1, min(500, int(limit)))
    return {"decisions": _ControlPlane.instance().audit.recent(limit)}


class _ProfileUpdate(BaseModel):
    profile: str


@router.post("/control-plane/profile")
async def control_plane_set_profile(
    body: _ProfileUpdate,
    user_id: str = Depends(require_role("admin")),
):
    try:
        _ControlPlane.instance().set_profile(body.profile)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    audit_log("admin.control_plane_profile_changed",
              user_id=user_id, success=True,
              details={"profile": body.profile})
    return {"profile": body.profile}


@router.post("/control-plane/emergency/exit")
async def control_plane_emergency_exit(
    user_id: str = Depends(require_role("admin")),
):
    did = _ControlPlane.instance().force_exit_emergency()
    audit_log("admin.control_plane_emergency_exit",
              user_id=user_id, success=did,
              details={"applied": did})
    return {"applied": did}


@router.get("/control-plane/rooms")
async def control_plane_rooms(user_id: str = Depends(require_role("admin"))):
    return {"rooms": _ControlPlane.instance().room_snapshot()}


class _ForceRoomMode(BaseModel):
    mode: str
    ttl_sec: int = 900
    reason: str = ""


@router.post("/control-plane/rooms/{room_id}/force")
async def control_plane_force_room(
    room_id: str,
    body: _ForceRoomMode,
    user_id: str = Depends(require_role("admin")),
):
    valid = {"p2p", "sfu", "relay", "audio-only", "chat-only"}
    if body.mode not in valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"mode must be one of {sorted(valid)}")
    ok = _ControlPlane.instance().force_room_mode(
        room_id, body.mode, body.ttl_sec, by=user_id, reason=body.reason)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="room not tracked")
    audit_log("admin.control_plane_room_force",
              user_id=user_id, success=True,
              details={"room_id": room_id, "mode": body.mode,
                       "ttl_sec": body.ttl_sec, "reason": body.reason})
    return {"room_id": room_id, "applied": True, "mode": body.mode}


@router.delete("/control-plane/rooms/{room_id}/force")
async def control_plane_clear_room_force(
    room_id: str,
    user_id: str = Depends(require_role("admin")),
):
    ok = _ControlPlane.instance().clear_room_override(room_id)
    audit_log("admin.control_plane_room_force_clear",
              user_id=user_id, success=ok,
              details={"room_id": room_id})
    return {"room_id": room_id, "cleared": ok}


class _RoomCritical(BaseModel):
    critical: bool


@router.post("/control-plane/rooms/{room_id}/priority")
async def control_plane_room_priority(
    room_id: str,
    body: _RoomCritical,
    user_id: str = Depends(require_role("admin")),
):
    ok = _ControlPlane.instance().set_room_critical(room_id, body.critical)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="room not tracked")
    audit_log("admin.control_plane_room_priority",
              user_id=user_id, success=True,
              details={"room_id": room_id, "critical": body.critical})
    return {"room_id": room_id, "critical": body.critical}


# Test-only endpoint for operator demos: registers a synthetic room so
# the per-room state machine has something to act on without a real call.
class _DemoRoomRegister(BaseModel):
    room_id: str
    kind: str = "voice"
    participants: int = 2


@router.post("/control-plane/rooms/demo/register")
async def control_plane_demo_register(
    body: _DemoRoomRegister,
    user_id: str = Depends(require_role("admin")),
):
    _ControlPlane.instance().register_room(
        body.room_id, body.kind, body.participants)
    return {"registered": body.room_id}


@router.delete("/control-plane/rooms/demo/{room_id}")
async def control_plane_demo_unregister(
    room_id: str,
    user_id: str = Depends(require_role("admin")),
):
    _ControlPlane.instance().unregister_room(room_id)
    return {"unregistered": room_id}


# ────────────────────────────────────────────────────────────────
# Placement — node registry + scoring
# ────────────────────────────────────────────────────────────────

from app.services.node_registry import get_registry as _get_registry
from app.services.placement import (
    preview_candidates as _preview_candidates,
    RoomRequest as _RoomRequest,
)


@router.get("/placement/nodes")
async def placement_nodes(user_id: str = Depends(require_role("admin"))):
    reg = _get_registry()
    reg.refresh_self_load()
    return {
        "nodes": reg.node_dicts(include_dead=True),
        "self_node_id": reg.self_node_id,
    }


class _PeerRegister(BaseModel):
    node_id:     str
    host:        str
    port:        int = 3000
    capability:  dict
    roles:       dict


@router.post("/placement/nodes/register")
async def placement_register_peer(
    body: _PeerRegister,
    user_id: str = Depends(require_role("admin")),
):
    reg = _get_registry()
    n = reg.register_peer(
        body.node_id, body.host, body.port, body.capability, body.roles)
    audit_log("admin.node_peer_registered",
              user_id=user_id, success=True,
              details={"node_id": body.node_id, "host": body.host})
    return {"registered": True, "node_id": n.node_id}


@router.delete("/placement/nodes/{node_id}")
async def placement_unregister_peer(
    node_id: str,
    user_id: str = Depends(require_role("admin")),
):
    ok = _get_registry().unregister(node_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="cannot remove self or unknown node")
    audit_log("admin.node_peer_unregistered",
              user_id=user_id, success=True, details={"node_id": node_id})
    return {"unregistered": True}


@router.get("/placement/preview")
async def placement_preview(
    kind: str = "audio",
    participants: int = 3,
    priority: str = "normal",
    user_id: str = Depends(require_role("admin")),
):
    req = _RoomRequest(
        kind=kind,
        participants_est=int(participants),
        priority=priority,
        creator_node_id=_get_registry().self_node_id,
    )
    return {"candidates": _preview_candidates(req), "request": {
        "kind": kind, "participants": participants, "priority": priority,
    }}


@router.get("/placement/capacity")
async def placement_capacity(user_id: str = Depends(require_role("admin"))):
    """Return per-node auto-computed capacity + current utilization."""
    reg = _get_registry()
    reg.refresh_self_load()
    return {
        "nodes": reg.node_dicts(include_dead=False),
        "self_node_id": reg.self_node_id,
    }


class _CapacityUpdate(BaseModel):
    overrides: dict


@router.patch("/placement/capacity")
async def placement_set_capacity(
    body: _CapacityUpdate,
    user_id: str = Depends(require_role("admin")),
):
    """Persist operator-overridden capacity numbers and recompute."""
    reg = _get_registry()
    reg.save_capacity_overrides(body.overrides or {})
    audit_log("admin.capacity_override_set",
              user_id=user_id, success=True,
              details={"overrides": body.overrides})
    return {"applied": True, "nodes": reg.node_dicts(include_dead=False)}


class _GossipPayload(BaseModel):
    node_id: str
    load: dict
    known_peers: list = []        # transitive discovery payload
    capability: dict = {}         # lets receiver auto-register unknown sender


# Peer-to-peer heartbeat + cluster mesh — NO auth, LAN-only by design.
_public_router = APIRouter(tags=["placement-gossip"])


@_public_router.post("/placement/gossip")
async def placement_gossip(body: _GossipPayload):
    """Receive gossip. If unknown node AND the payload carries its own
    capability, auto-register it (seamless mesh join)."""
    reg = _get_registry()
    ok = reg.heartbeat(body.node_id, body.load)
    if not ok:
        # Auto-accept unknown peers if they included self-capability.
        cap = getattr(body, "capability", None) or {}
        if cap:
            try:
                reg.register_peer(
                    node_id=body.node_id,
                    host=cap.get("host", "unknown"),
                    port=int(cap.get("port", 0)),
                    capability=cap,
                    roles=cap.get("roles", {}),
                    capacity=cap.get("capacity", {}),
                )
                reg.heartbeat(body.node_id, body.load)
                return {"accepted": True, "joined": True}
            except Exception:
                pass
        return {"accepted": False, "reason": "unknown_node"}
    # Absorb known_peers list if gossip carried one — transitive discovery.
    kp = getattr(body, "known_peers", None) or []
    if kp:
        try:
            from app.services.cluster_mesh import get_mesh
            get_mesh().absorb_gossip_known_peers(kp)
        except Exception:
            pass
    return {"accepted": True}


# ── Expose the public router ────────────────────────────────────
router.include_router(_public_router)


# ────────────────────────────────────────────────────────────────
# Audit Log Query
# ────────────────────────────────────────────────────────────────


@router.get("/audit-logs")
async def query_audit_logs(
    event: str | None = None,
    target_user_id: str | None = None,
    success: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Query persistent audit logs with filtering and pagination.

    Query params:
      - event: prefix match on event name (e.g. "auth." or "admin.")
      - target_user_id: filter by the user the event is about
      - success: True/False to filter by outcome
      - since/until: ISO 8601 timestamps
      - limit: max 500 (default 100)
      - offset: pagination
    """
    from datetime import datetime
    from sqlalchemy import and_, func
    from app.models.audit_log import AuditLog

    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    conditions = []
    if event:
        conditions.append(AuditLog.event.like(f"{event}%"))
    if target_user_id:
        conditions.append(AuditLog.user_id == target_user_id)
    if success is not None:
        conditions.append(AuditLog.success == bool(success))
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            conditions.append(AuditLog.occurred_at >= since_dt)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid 'since' timestamp")
    if until:
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            conditions.append(AuditLog.occurred_at <= until_dt)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid 'until' timestamp")

    base_query = select(AuditLog)
    if conditions:
        base_query = base_query.where(and_(*conditions))

    # Total count
    count_query = select(func.count(AuditLog.id))
    if conditions:
        count_query = count_query.where(and_(*conditions))
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Page
    page_query = (
        base_query.order_by(AuditLog.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(page_query)
    rows = result.scalars().all()

    audit_log(
        "admin.audit_logs_queried",
        user_id=user_id,
        success=True,
        details={
            "filters": {
                "event": event,
                "target_user_id": target_user_id,
                "success": success,
                "since": since,
                "until": until,
            },
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
        },
    )

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": [r.to_dict() for r in rows],
    }


@router.get("/audit-logs/events")
async def list_audit_event_types(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Return distinct audit event names with counts (last 30 days).
    Useful for building filter dropdowns in the admin UI.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from app.models.audit_log import AuditLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(AuditLog.event, func.count(AuditLog.id))
        .where(AuditLog.occurred_at >= cutoff)
        .group_by(AuditLog.event)
        .order_by(func.count(AuditLog.id).desc())
    )
    result = await db.execute(stmt)
    rows = result.all()
    return {
        "since": cutoff.isoformat(),
        "events": [{"event": ev, "count": count} for ev, count in rows],
    }


# ────────────────────────────────────────────────────────────────
# Federation — admin visibility & key management
# ────────────────────────────────────────────────────────────────


@router.get("/federation/status")
async def federation_status(
    user_id: str = Depends(require_role("admin")),
):
    """Snapshot: whether federation is on, secret health, live peers,
    and active multi-hop relay sessions.

    The secret is NEVER returned in full — only its length and a short
    fingerprint — so this endpoint is safe to surface in dashboards.
    """
    from hashlib import sha256
    from app.core.config import get_settings
    from app.services.discovery_service import get_server_id
    from app.services.peer_registry import peer_registry
    from app.services.relay_worker import relay_manager

    settings = get_settings()
    secret = settings.FEDERATION_SECRET or ""
    fingerprint = (
        sha256(secret.encode()).hexdigest()[:12] if secret else ""
    )

    peers = await peer_registry.list(include_stale=False)
    return {
        "enabled": bool(settings.FEDERATION_ENABLED),
        "has_secret": bool(secret),
        "secret_length": len(secret),
        "secret_fingerprint": fingerprint,
        "replay_window_seconds": settings.FEDERATION_REPLAY_WINDOW_SECONDS,
        "peer_timeout_seconds": settings.FEDERATION_PEER_TIMEOUT_SECONDS,
        "server_id": get_server_id(),
        "peers_live": len(peers),
        "peers": [
            {
                "server_id": p.server_id,
                "name": p.name,
                "host": p.host,
                "port": p.port,
                "age_seconds": round(p.age_seconds, 2),
            }
            for p in peers
        ],
        "relay_sessions": relay_manager.list_sessions(),
    }


class GenerateSecretRequest(BaseModel):
    length: int = 64


@router.post("/federation/generate-secret")
async def federation_generate_secret(
    body: GenerateSecretRequest | None = None,
    user_id: str = Depends(require_role("admin")),
):
    """Generate a cryptographically-strong candidate `FEDERATION_SECRET`.

    We don't persist this automatically — the admin must copy it into
    every peer's `.env` and restart, because a secret mid-rotation would
    silently reject valid peers. The returned string is CSPRNG-drawn
    from [A-Za-z0-9] and at least 64 chars long to match the rest of
    Helen's identifier policy.
    """
    import secrets as _secrets
    length = max(64, min(256, (body.length if body else 64)))
    alphabet = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
    )
    candidate = "".join(_secrets.choice(alphabet) for _ in range(length))
    audit_log(
        "admin.federation_secret_generated",
        user_id=user_id,
        success=True,
        details={"length": length},
    )
    return {
        "secret": candidate,
        "length": length,
        "note": (
            "Copy into FEDERATION_SECRET in the .env of EVERY peer, then "
            "restart. Unmatched secrets silently reject peer requests."
        ),
    }


@router.get("/federation/topology")
async def federation_topology(
    user_id: str = Depends(require_role("admin")),
):
    """Crawl the federation graph and return the observed topology.

    Useful for diagnosing why a relay chain can or can't reach a given
    server. Blocking call — may take a few seconds on large meshes.
    """
    from app.core.config import get_settings
    if not get_settings().FEDERATION_ENABLED:
        raise HTTPException(status_code=503, detail="federation disabled")
    from app.services.relay_path import discover_topology
    graph = await discover_topology()
    return {"graph": graph, "node_count": len(graph)}


@router.get("/federation/metrics")
async def federation_metrics(
    user_id: str = Depends(require_role("admin")),
):
    """Process-local federation metrics (resets on restart).

    Includes HMAC verification counts, relay allocation stats, presence
    fan-out counts, circuit-breaker state per peer. Intended to be
    scraped by dashboards or spot-checked by operators.
    """
    from app.services.federation_metrics import snapshot
    return snapshot()


# ────────────────────────────────────────────────────────────────
# Dead Letter Queue (failed-fanout review + replay)
# ────────────────────────────────────────────────────────────────


@router.get("/dlq")
async def list_dlq_entries(
    status_filter: str | None = None,
    kind_filter: str | None = None,
    limit: int = 100,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List DLQ entries — failed message-fanout / webhook deliveries
    that the system gave up on and recorded for human review.

    Query params:
      status_filter: pending | replayed | abandoned | replaying
      kind_filter:   fanout | webhook | …
      limit:         max rows returned (default 100, hard cap 1000)
    """
    from sqlalchemy import desc, select as _sel
    from app.models.message_dead_letter import MessageDeadLetter

    capped = max(1, min(int(limit or 100), 1000))
    stmt = _sel(MessageDeadLetter).order_by(desc(MessageDeadLetter.created_at))
    if status_filter:
        stmt = stmt.where(MessageDeadLetter.status == status_filter)
    if kind_filter:
        stmt = stmt.where(MessageDeadLetter.kind == kind_filter)
    stmt = stmt.limit(capped)

    rows = (await db.execute(stmt)).scalars().all()
    return {
        "entries": [
            {
                "id":              r.id,
                "kind":            r.kind,
                "status":          r.status,
                "reason":          r.reason,
                "error":           r.error,
                "channel_id":      r.channel_id,
                "sender_id":       r.sender_id,
                "message_id":      r.message_id,
                "attempt_count":   r.attempt_count,
                "created_at":      r.created_at.isoformat() if r.created_at else None,
                "last_attempt_at": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
                "resolved_at":     r.resolved_at.isoformat() if r.resolved_at else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/sfu/status")
async def get_sfu_status(
    user_id: str = Depends(require_role("admin")),
):
    """SFU launcher snapshot + live health probe.

    Returns the supervisor's view of the mediasoup-worker process plus
    a freshly-issued HTTP probe of its control plane. Lets operators see:
      - Is auto-launch enabled?
      - Is the Node process running?
      - Is mediasoup actually answering RPCs?
      - Restart counts + last-error string for diagnostics.

    The probe runs with a tight 1.5s timeout so this endpoint is safe
    to poll from the admin dashboard at a few-second cadence.
    """
    try:
        from app.services.sfu_launcher import sfu_launcher
        snapshot = sfu_launcher.snapshot()
        is_healthy = await sfu_launcher.is_healthy()
        snapshot["healthy"] = is_healthy
        audit_log("admin.sfu_status_requested", user_id=user_id, success=True)
        return snapshot
    except Exception as e:
        logger.error("get_sfu_status_error", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve SFU status: {e}",
        )


@router.post("/dlq/{entry_id}/replay")
async def replay_dlq_entry(
    entry_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Manually retry a DLQ entry. Returns the updated row.

    Already-replayed or already-abandoned entries are returned as-is
    without re-running the side-effect.
    """
    from app.services.dead_letter_service import DeadLetterService
    row = await DeadLetterService.replay_entry(db, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="DLQ entry not found")
    audit_log("admin.dlq.replay", user_id=user_id,
              details={"target": entry_id, "kind": row.kind,
                       "result_status": row.status})
    return {
        "id":     row.id,
        "kind":   row.kind,
        "status": row.status,
        "error":  row.error,
        "attempt_count": row.attempt_count,
    }


# ────────────────────────────────────────────────────────────────
# Crash reports — local SQLite store fed by services/crash_reporter
# ────────────────────────────────────────────────────────────────


@router.get("/crashes")
async def list_crashes(
    limit: int = 100,
    level: str | None = None,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.crash_reporter import get_reporter
    rep = get_reporter()
    if rep is None:
        return {"events": [], "installed": False}
    events = rep.store.list_recent(limit=limit, level=level)
    audit_log("admin.crashes_listed", user_id=user_id, success=True)
    return {"events": events, "installed": True, "count": len(events)}


@router.get("/crashes/{event_id}")
async def get_crash(
    event_id: str,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.crash_reporter import get_reporter
    rep = get_reporter()
    if rep is None:
        raise HTTPException(status_code=503, detail="Crash reporter not installed")
    evt = rep.store.get(event_id)
    if not evt:
        raise HTTPException(status_code=404, detail="Crash event not found")
    audit_log("admin.crash_viewed", user_id=user_id, success=True,
              details={"target": event_id})
    return evt


@router.delete("/crashes/older-than/{days}")
async def purge_crashes(
    days: int,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.crash_reporter import get_reporter
    rep = get_reporter()
    if rep is None:
        raise HTTPException(status_code=503, detail="Crash reporter not installed")
    if days < 1:
        raise HTTPException(status_code=400, detail="days must be >= 1")
    deleted = rep.store.purge_older_than(days=days)
    audit_log("admin.crashes_purged", user_id=user_id,
              details={"days": days, "deleted": deleted})
    return {"deleted": deleted, "days": days}


# ────────────────────────────────────────────────────────────────
# Audit chain — tamper-evident hash chain over audit_log events
# ────────────────────────────────────────────────────────────────


@router.get("/audit-chain/head")
async def audit_chain_head(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.audit_chain import get_audit_chain
    chain = get_audit_chain()
    if chain is None:
        return {"configured": False}
    h = chain.head()
    if h is None:
        return {"configured": True, "empty": True}
    return {
        "configured": True,
        "head": {
            "seq": h.seq, "timestamp": h.timestamp,
            "actor": h.actor, "action": h.action,
            "target": h.target, "chain_hash": h.chain_hash,
        },
    }


@router.post("/audit-chain/verify")
async def audit_chain_verify(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.audit_chain import get_audit_chain
    chain = get_audit_chain()
    if chain is None:
        raise HTTPException(status_code=503, detail="audit chain not configured")
    ok, broken_at, msg = chain.verify()
    audit_log("admin.audit_chain_verified", user_id=user_id,
              success=ok,
              details={"broken_at": broken_at, "msg": msg})
    return {"ok": ok, "broken_at_seq": broken_at, "message": msg}


@router.get("/audit-chain/entries")
async def audit_chain_entries(
    actor: str | None = None,
    action: str | None = None,
    since: float | None = None,
    limit: int = 200,
    user_id: str = Depends(require_role("admin")),
):
    from app.services.audit_chain import get_audit_chain
    chain = get_audit_chain()
    if chain is None:
        raise HTTPException(status_code=503, detail="audit chain not configured")
    if limit < 1 or limit > 5000:
        raise HTTPException(status_code=400, detail="limit out of range")
    entries = []
    for e in chain.filter(actor=actor, action=action, since=since, limit=limit):
        entries.append({
            "seq": e.seq, "timestamp": e.timestamp,
            "actor": e.actor, "action": e.action, "target": e.target,
            "payload": e.payload,
            "chain_hash": e.chain_hash,
        })
    return {"entries": entries, "count": len(entries)}


# ────────────────────────────────────────────────────────────────
# Optional transport backends — visibility into the new adapters
# ────────────────────────────────────────────────────────────────


@router.get("/transports/nats/status")
async def transport_nats_status(
    user_id: str = Depends(require_role("admin")),
):
    """Returns whether the NATS adapter is configured + active."""
    from app.services.nats_adapter import get_nats
    nats = get_nats()
    if nats is None:
        return {"configured": False}
    return {"configured": True, **nats.stats()}


@router.get("/transports/mqtt/status")
async def transport_mqtt_status(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.mqtt_adapter import get_mqtt
    mqtt = get_mqtt()
    if mqtt is None:
        return {"configured": False}
    return {"configured": True, **mqtt.stats()}


@router.get("/transports/grpc/status")
async def transport_grpc_status(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.grpc_federation import get_grpc_federation
    grpc = get_grpc_federation()
    if grpc is None:
        return {"configured": False}
    return {
        "configured": True,
        "bind_host": grpc.bind_host,
        "bind_port": grpc.bind_port,
        "tls": bool(grpc.cert_path),
        "running": grpc._server is not None,
    }


@router.get("/transports/wireguard/status")
async def transport_wireguard_status(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.wireguard_manager import get_wireguard
    wg = get_wireguard()
    if wg is None:
        return {"configured": False}
    return {"configured": True, **wg.stats()}


@router.get("/transports/zeromq/status")
async def transport_zeromq_status(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.zeromq_adapter import get_zeromq
    z = get_zeromq()
    if z is None:
        return {"configured": False}
    return {"configured": True, **z.stats()}


@router.get("/transports/rabbitmq/status")
async def transport_rabbitmq_status(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.rabbitmq_adapter import get_rabbitmq
    r = get_rabbitmq()
    if r is None:
        return {"configured": False}
    return {"configured": True, **r.stats()}


@router.get("/transports/ssh/status")
async def transport_ssh_status(
    user_id: str = Depends(require_role("admin")),
):
    from app.services.ssh_tunnel_manager import get_ssh_tunnels
    s = get_ssh_tunnels()
    if s is None:
        return {"configured": False}
    return {"configured": True, **s.stats()}


@router.get("/transports/backends")
async def transport_backends_summary(
    user_id: str = Depends(require_role("admin")),
):
    """One-shot summary so the admin UI can render a backends table."""
    import os
    from app.services.nats_adapter import get_nats
    from app.services.mqtt_adapter import get_mqtt
    from app.services.grpc_federation import get_grpc_federation
    from app.services.wireguard_manager import get_wireguard
    from app.services.zeromq_adapter import get_zeromq
    from app.services.rabbitmq_adapter import get_rabbitmq
    from app.services.ssh_tunnel_manager import get_ssh_tunnels
    return {
        "broker_backend": os.environ.get("HELEN_BROKER_BACKEND", "redis"),
        "federation_backend": os.environ.get(
            "HELEN_FEDERATION_BACKEND", "http",
        ),
        "vpn_backend": os.environ.get("HELEN_VPN_BACKEND", ""),
        "mesh_topology": os.environ.get("HELEN_MESH_TOPOLOGY", "mesh"),
        "ssh_tunnels_enabled": os.environ.get(
            "HELEN_SSH_TUNNELS_ENABLED", "",
        ).lower() in ("1", "true", "yes"),
        "active": {
            "nats": get_nats() is not None,
            "mqtt": get_mqtt() is not None,
            "zeromq": get_zeromq() is not None,
            "rabbitmq": get_rabbitmq() is not None,
            "grpc_federation": get_grpc_federation() is not None,
            "wireguard": get_wireguard() is not None,
            "ssh_tunnels": get_ssh_tunnels() is not None,
        },
    }


# ── Group 2: WAN port-forward, TURN health, recursive DNS ──────────


@router.get("/wan/portmap/status")
async def wan_portmap_status(
    user_id: str = Depends(require_role("admin")),
):
    """Live state of the WAN port-forward manager: UPnP outcome,
    advertised external IP, vendor-specific manual instructions for
    the operator, and last reachability-probe results from peers."""
    from app.services.wan_port_forward import get_wan_portmap
    mgr = get_wan_portmap()
    if mgr is None:
        return {"configured": False}
    return {"configured": True, **mgr.status()}


@router.post("/wan/portmap/refresh")
async def wan_portmap_refresh(
    user_id: str = Depends(require_role("admin")),
):
    """Force an immediate UPnP re-map + reachability probe."""
    from app.services.wan_port_forward import get_wan_portmap
    mgr = get_wan_portmap()
    if mgr is None:
        raise HTTPException(status_code=404,
                              detail="WAN portmap manager not configured")
    return await mgr.refresh_now()


class _ProbeBackBody(BaseModel):
    target_ip: str
    target_port: int
    protocol: str = "TCP"


@router.post("/wan/probe-back")
async def wan_probe_back(
    body: _ProbeBackBody,
    user_id: str = Depends(require_role("admin")),
):
    """Reachability-probe endpoint a peer can call to verify our
    advertised external IP+port is actually open. Returns
    ``{reachable, latency_ms, error}``."""
    from app.services.wan_port_forward import probe_back_locally
    return probe_back_locally(
        body.target_ip, body.target_port, body.protocol,
    )


@router.get("/transports/turn/health")
async def turn_health(
    user_id: str = Depends(require_role("admin")),
    host: str = "127.0.0.1",
    port: int = 3478,
):
    """STUN binding + TURN allocate self-test against the configured
    TURN server. Hostname/port can be overridden via query string;
    defaults probe the bundled coturn instance."""
    from app.services.turn_health import check_turn_health, health_to_dict
    h = await check_turn_health(host, port=port)
    return health_to_dict(h)


@router.get("/dns/stats")
async def dns_stats(
    user_id: str = Depends(require_role("admin")),
):
    """Pi-hole-style DNS counters from the recursive resolver: total
    queries, cache hits, blocks, top-20 queried domains. Returns
    ``{configured: false}`` when the recursive resolver isn't
    running (Helen-Server alone, no Helen-Router on this host)."""
    try:
        # Recursive DNS lives in Helen-Router; try to import lazily.
        import sys
        import importlib.util
        from pathlib import Path
        if "Helen_Router_dns_compat" not in sys.modules:
            here = Path(__file__).resolve()
            for parent in here.parents:
                cand = parent / "Helen-Router" / "app" / "recursive_dns.py"
                if cand.is_file():
                    spec = importlib.util.spec_from_file_location(
                        "Helen_Router_dns_compat", str(cand),
                    )
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        sys.modules["Helen_Router_dns_compat"] = mod
                        spec.loader.exec_module(mod)
                        break
            else:
                return {"configured": False,
                        "error": "recursive_dns module not found"}
        from app.core.recursive_dns_singleton import get_recursive_dns  # noqa
        srv = get_recursive_dns()
        if srv is None:
            return {"configured": False}
        return {
            "configured": True,
            "blocklist_size": len(srv.blocklist),
            "cache_size": srv.cache.size(),
            "upstreams": [f"{h}:{p}" for h, p in srv.upstreams],
            "stats": srv.stats.to_dict(),
        }
    except ImportError:
        return {"configured": False,
                "error": "recursive DNS singleton not wired"}
