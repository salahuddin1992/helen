"""
Admin — Unified health aggregation (Phase 2 / Module K).

Single endpoint that gathers every signal the operations dashboard wants
into one round-trip:

    GET /api/admin/health/aggregate

Each section is wrapped in try/except so a single broken probe never
fails the whole aggregate; failures are reported as ``{"status":"error",
"error":...}`` per section.
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.services.metrics_collector import metrics_collector

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/health", tags=["admin-phase2"])


_STARTED_AT = time.time()


# ── Section probes ────────────────────────────────────────

async def _server_section() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "uptime_sec": time.time() - _STARTED_AT,
        "host": s.HOST,
        "port": s.PORT,
        "debug": s.DEBUG,
        "log_level": s.LOG_LEVEL,
        "server_name": s.SERVER_NAME,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "pid": os.getpid(),
    }


async def _db_section(db: AsyncSession) -> dict[str, Any]:
    s = get_settings()
    out: dict[str, Any] = {"backend": s.DB_BACKEND}
    try:
        if s.DB_BACKEND == "sqlite":
            sqlite_path = Path(s.SQLITE_PATH)
            if sqlite_path.exists():
                out["size_bytes"] = sqlite_path.stat().st_size
                out["path"] = str(sqlite_path)
        # Quick cheap probes — count users + channels via raw COUNT
        from app.models.user import User
        out["users_total"] = (await db.execute(
            select(func.count()).select_from(User)
        )).scalar_one()
        try:
            from app.models.message import Message
            out["messages_total"] = (await db.execute(
                select(func.count()).select_from(Message)
            )).scalar_one()
        except Exception:
            out["messages_total"] = None
        try:
            from app.models.channel import Channel
            out["channels_total"] = (await db.execute(
                select(func.count()).select_from(Channel)
            )).scalar_one()
        except Exception:
            out["channels_total"] = None

        out["last_backup"] = _last_backup_iso()
        out["status"] = "ok"
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)
    return out


def _last_backup_iso() -> str | None:
    s = get_settings()
    root = Path(s.PROJECT_ROOT)
    backups = root / "data" / "backups"
    if not backups.exists():
        return None
    files = [p for p in backups.iterdir() if p.is_file()]
    if not files:
        return None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(latest.stat().st_mtime,
                                  tz=timezone.utc).isoformat()


async def _federation_section() -> dict[str, Any]:
    try:
        from app.services.federation import federation_service
        return {
            "status": "ok",
            "peer_count": len(getattr(federation_service, "peers", []) or []),
            "enabled": True,
        }
    except Exception:
        return {"status": "n/a", "peer_count": 0, "enabled": False}


async def _turn_section() -> dict[str, Any]:
    try:
        from app.services.turn import turn_service
        sessions = getattr(turn_service, "active_sessions", lambda: 0)
        return {
            "status": "ok",
            "active_sessions": int(sessions() if callable(sessions) else sessions),
            "bandwidth_bps": getattr(turn_service, "current_bandwidth_bps", 0),
        }
    except Exception:
        return {"status": "n/a", "active_sessions": 0}


async def _mediasoup_section() -> dict[str, Any]:
    try:
        from app.services.sfu import sfu_service
        workers = getattr(sfu_service, "workers", [])
        return {
            "status": "ok" if workers else "idle",
            "workers": [
                {"pid": getattr(w, "pid", None),
                 "load": getattr(w, "load", 0.0)}
                for w in workers
            ],
        }
    except Exception:
        return {"status": "n/a", "workers": []}


async def _nat_section() -> dict[str, Any]:
    try:
        from app.services.connectivity import orchestrator
        st = orchestrator.status()
        return {
            "status": "ok",
            "nat_type": st.get("nat_type"),
            "public_ip": st.get("public_ip"),
            "lan_ip": st.get("lan_ip"),
            "active_strategies": st.get("active", []),
        }
    except Exception:
        return {"status": "n/a"}


async def _resources_section() -> dict[str, Any]:
    try:
        metrics_collector.start()
        snap = metrics_collector.snapshot()
        m = snap.get("metrics", {})
        return {
            "status": "ok",
            "cpu_percent": m.get("cpu_percent"),
            "memory_percent": m.get("memory_percent"),
            "memory_mb": m.get("memory_mb"),
            "disk_io_read": m.get("disk_io_read"),
            "disk_io_write": m.get("disk_io_write"),
            "network_recv": m.get("network_recv"),
            "network_sent": m.get("network_sent"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def _disk_section() -> dict[str, Any]:
    try:
        import shutil
        s = get_settings()
        usage = shutil.disk_usage(str(s.PROJECT_ROOT))
        return {
            "status": "ok",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_percent": (usage.free / usage.total) * 100 if usage.total else 0,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def _alarms_section() -> dict[str, Any]:
    """Cheap composite alarm panel — turn known signals into alerts."""
    alarms: list[dict[str, Any]] = []
    try:
        snap = metrics_collector.snapshot()
        m = snap.get("metrics") or {}
        if (m.get("cpu_percent") or 0) > 90:
            alarms.append({"level": "warn", "key": "cpu_high",
                           "message": f"CPU {m['cpu_percent']:.0f}%"})
        if (m.get("memory_percent") or 0) > 90:
            alarms.append({"level": "warn", "key": "memory_high",
                           "message": f"Memory {m['memory_percent']:.0f}%"})
        if (m.get("queue_depth") or 0) > 100:
            alarms.append({"level": "warn", "key": "queue_backlog",
                           "message": f"Queue depth {m['queue_depth']:.0f}"})
    except Exception:
        pass
    try:
        from app.services.audit_chain import get_audit_chain
        chain = get_audit_chain()
        if chain is not None:
            ok, broken_at, msg = chain.verify()
            if not ok:
                alarms.append({"level": "crit", "key": "audit_chain_broken",
                               "message": f"audit chain broken at seq={broken_at}: {msg}"})
    except Exception:
        pass
    return {"status": "ok", "alarms": alarms, "count": len(alarms)}


# ── Aggregate endpoint ────────────────────────────────────

@router.get("/aggregate")
async def aggregate(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Run every probe in parallel."""
    server, db_, fed, turn, ms, nat, res, disk, alarms = await asyncio.gather(
        _server_section(),
        _db_section(db),
        _federation_section(),
        _turn_section(),
        _mediasoup_section(),
        _nat_section(),
        _resources_section(),
        _disk_section(),
        _alarms_section(),
        return_exceptions=False,
    )
    return {
        "generated_at": time.time(),
        "sections": {
            "server": server,
            "database": db_,
            "federation": fed,
            "turn": turn,
            "mediasoup": ms,
            "nat": nat,
            "resources": res,
            "disk": disk,
            "alarms": alarms,
        },
    }
