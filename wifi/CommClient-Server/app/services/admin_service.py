"""
Admin operations service — server statistics, user management, system metrics.
Provides server diagnostics, call state inspection, and system-level operations.

psutil is optional — gracefully degrades if unavailable (used for CPU/memory metrics).
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.channel import Channel
from app.models.message import Message
from app.models.session import UserSession
from app.models.file import FileRecord
from app.models.user import User
from app.services.backup_service import backup_service
from app.services.call_service import call_service
from app.services.metrics_service import metrics_service
from app.services.presence_service import presence_service
from app.socket.server import sio

logger = get_logger(__name__)

# Optional: psutil for system metrics
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil_not_available", message="CPU and memory metrics will be unavailable")


def get_lan_ip() -> str:
    """Get the primary LAN IP address of this host."""
    try:
        # Connect to a non-routable address (doesn't actually send packets)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class AdminService:
    """Centralized admin operations and server diagnostics."""

    def __init__(self):
        self._start_time: datetime = datetime.now(timezone.utc)
        self._lock = asyncio.Lock()
        logger.info("admin_service_initialized")

    async def get_server_stats(self) -> dict[str, Any]:
        """
        Return comprehensive server statistics and health metrics.
        Includes: uptime, user counts, resource usage, database size, call state.
        """
        async with self._lock:
            try:
                uptime_seconds = (datetime.now(timezone.utc) - self._start_time).total_seconds()
                online_users = await presence_service.get_online_user_ids()
                metrics = await metrics_service.get_all()
                db_size = await backup_service.get_db_size()

                # System metrics (psutil)
                memory_mb = 0.0
                cpu_percent = 0.0
                if PSUTIL_AVAILABLE:
                    try:
                        process = psutil.Process(os.getpid())
                        memory_mb = process.memory_info().rss / (1024 * 1024)
                        cpu_percent = process.cpu_percent(interval=0.1)
                    except Exception as e:
                        logger.warning("psutil_metrics_failed", error=str(e))

                # Active socket connections (managed by socket.io)
                try:
                    active_connections = len(sio.manager.rooms.get("", set())) if sio.manager else 0
                except Exception:
                    active_connections = 0

                # Real DB row counts. We open a fresh session here rather
                # than threading one in via FastAPI's `Depends(get_db)`
                # because admin_service is also called from socket-event
                # handlers and background timers that don't have a request
                # scope. count() is a single indexed query each — cheap.
                total_users = 0
                total_channels = 0
                total_messages_db = 0
                total_files_db = 0
                try:
                    async with async_session_factory() as db:
                        total_users = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
                        total_channels = (await db.execute(select(func.count()).select_from(Channel))).scalar() or 0
                        total_messages_db = (await db.execute(select(func.count()).select_from(Message))).scalar() or 0
                        total_files_db = (await db.execute(select(func.count()).select_from(FileRecord))).scalar() or 0
                except Exception as exc:
                    logger.warning("admin_stats_db_count_failed", error=str(exc))

                stats = {
                    "uptime_seconds": uptime_seconds,
                    "total_users": total_users,
                    "online_users": len(online_users),
                    "total_channels": total_channels,
                    # `total_messages` is the cumulative on-disk count;
                    # `messages_sent_total` is the in-process counter
                    # (resets on restart). Prefer the DB count as the
                    # canonical "how big is this server" metric.
                    "total_messages": total_messages_db or metrics.get("messages_sent_total", 0),
                    "total_files":    total_files_db    or metrics.get("files_uploaded_total", 0),
                    "total_calls": metrics.get("calls_initiated_total", 0),
                    "db_size_bytes": db_size,
                    "active_socket_connections": active_connections,
                    "server_version": "1.0.0",
                    "hostname": socket.gethostname(),
                    "lan_ip": get_lan_ip(),
                    "memory_usage_mb": round(memory_mb, 2),
                    "cpu_percent": round(cpu_percent, 2),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                logger.info("server_stats_collected", uptime_seconds=uptime_seconds, online_users=len(online_users))
                return stats

            except Exception as e:
                logger.error("get_server_stats_failed", error=str(e))
                raise

    async def get_active_calls(self) -> list[dict[str, Any]]:
        """Return list of currently active calls with participant info."""
        try:
            calls = []
            for call_id, call in call_service._active_calls.items():
                calls.append(call.to_dict())
            logger.info("active_calls_collected", count=len(calls))
            return calls
        except Exception as e:
            logger.error("get_active_calls_failed", error=str(e))
            return []

    async def kick_user(self, user_id: str) -> bool:
        """
        Force disconnect all socket connections for a user.
        Returns True if at least one connection was found and disconnected.
        """
        async with self._lock:
            try:
                user_sids = await presence_service.get_socket_ids(user_id)
                if not user_sids:
                    logger.warning("kick_user_no_connections", user_id=user_id)
                    return False

                for sid in user_sids:
                    try:
                        await sio.disconnect(sid, namespace="/")
                        logger.info("user_kicked", user_id=user_id, sid=sid)
                    except Exception as e:
                        logger.warning("kick_user_disconnect_failed", user_id=user_id, sid=sid, error=str(e))

                return True

            except Exception as e:
                logger.error("kick_user_failed", user_id=user_id, error=str(e))
                return False

    async def ban_user(self, db: AsyncSession, user_id: str) -> bool:
        """
        Soft-ban a user by setting is_active = False.
        Does not delete the user or their data.
        """
        try:
            from app.models.user import User
            from sqlalchemy import update

            stmt = update(User).where(User.id == user_id).values(is_active=False)
            result = await db.execute(stmt)
            await db.commit()

            if result.rowcount > 0:
                logger.warning("user_banned", user_id=user_id)
                # Also kick the user if connected
                await self.kick_user(user_id)
                return True

            logger.warning("ban_user_not_found", user_id=user_id)
            return False

        except Exception as e:
            logger.error("ban_user_failed", user_id=user_id, error=str(e))
            return False

    async def unban_user(self, db: AsyncSession, user_id: str) -> bool:
        """
        Unban a user by setting is_active = True.
        """
        try:
            from app.models.user import User
            from sqlalchemy import update

            stmt = update(User).where(User.id == user_id).values(is_active=True)
            result = await db.execute(stmt)
            await db.commit()

            if result.rowcount > 0:
                logger.info("user_unbanned", user_id=user_id)
                return True

            logger.warning("unban_user_not_found", user_id=user_id)
            return False

        except Exception as e:
            logger.error("unban_user_failed", user_id=user_id, error=str(e))
            return False

    async def get_audit_log(
        self, db: AsyncSession, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Return recent persistent audit-log rows from the ``audit_logs``
        table. Wired into the same model the ``/api/admin/audit-logs``
        endpoint queries directly — this method is a service-layer
        convenience for in-process callers.

        Order: newest first. Returns up to ``limit`` rows.
        """
        from app.models.audit_log import AuditLog
        from sqlalchemy import select, desc

        try:
            stmt = (
                select(AuditLog)
                .order_by(desc(AuditLog.timestamp))
                .limit(max(1, min(int(limit), 1000)))
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "id": str(r.id),
                    "event": r.event,
                    "user_id": r.user_id,
                    "ip_address": getattr(r, "ip_address", None),
                    "success": bool(r.success),
                    "details": r.details or {},
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]
        except Exception as e:
            # Schema can lag on first boot before alembic stamps; fall
            # back to an empty list rather than raising into the caller.
            logger.warning("audit_log_query_failed", error=str(e))
            return []

    async def cleanup_expired_sessions(self, db: AsyncSession) -> int:
        """
        Delete JWT sessions that have expired.
        Returns number of sessions deleted.
        """
        try:
            now = datetime.now(timezone.utc)

            # Delete sessions where expires_at < now
            stmt = delete(UserSession).where(UserSession.expires_at < now)
            result = await db.execute(stmt)
            await db.commit()

            deleted = result.rowcount
            logger.info("cleanup_expired_sessions_completed", deleted_count=deleted)
            return deleted

        except Exception as e:
            logger.error("cleanup_expired_sessions_failed", error=str(e))
            return 0

    async def cleanup_orphaned_files(self, db: AsyncSession) -> int:
        """
        Delete file records that are not referenced by any message.
        Returns number of file records deleted.

        Currently a no-op to prevent accidental data loss — proper
        implementation needs auditing and careful FK traversal.
        """
        logger.info(
            "cleanup_orphaned_files_deferred",
            reason="requires careful implementation",
        )
        return 0


# Singleton instance
admin_service = AdminService()
