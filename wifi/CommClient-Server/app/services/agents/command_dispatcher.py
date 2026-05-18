"""
Module L — Command dispatcher.

Holds the in-memory map of agent_id → live WebSocket and forwards admin
commands to the appropriate connection. Tracks per-command timeouts and
retries; persists results into `agent_commands` rows.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.agent import AgentCommand, AgentEvent

logger = get_logger(__name__)


class _AgentConnection:
    """A registered control connection for a single agent."""

    __slots__ = ("agent_id", "websocket", "send_lock", "event_subs")

    def __init__(self, agent_id: str, websocket: WebSocket) -> None:
        self.agent_id = agent_id
        self.websocket = websocket
        self.send_lock = asyncio.Lock()
        # Admin-side event WebSockets subscribed to this agent.
        self.event_subs: set[WebSocket] = set()

    async def send(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_text(json.dumps(payload))


class CommandDispatcher:
    """Process-local registry of live agent control connections."""

    def __init__(self) -> None:
        self._conns: Dict[str, _AgentConnection] = {}
        self._lock = asyncio.Lock()

    # ── Connection lifecycle ────────────────────────────────

    async def attach(self, agent_id: str, websocket: WebSocket) -> _AgentConnection:
        async with self._lock:
            prev = self._conns.get(agent_id)
            if prev:
                try:
                    await prev.websocket.close()
                except Exception:
                    pass
            conn = _AgentConnection(agent_id, websocket)
            self._conns[agent_id] = conn
        logger.info("agent_ws_attached", agent_id=agent_id)
        return conn

    async def detach(self, agent_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            conn = self._conns.get(agent_id)
            if conn and conn.websocket is websocket:
                self._conns.pop(agent_id, None)
        logger.info("agent_ws_detached", agent_id=agent_id)

    def get(self, agent_id: str) -> Optional[_AgentConnection]:
        return self._conns.get(agent_id)

    def is_online(self, agent_id: str) -> bool:
        return agent_id in self._conns

    # ── Event subscription ──────────────────────────────────

    async def subscribe_events(self, agent_id: str, ws: WebSocket) -> None:
        conn = self._conns.get(agent_id)
        if conn:
            conn.event_subs.add(ws)

    async def unsubscribe_events(self, agent_id: str, ws: WebSocket) -> None:
        conn = self._conns.get(agent_id)
        if conn and ws in conn.event_subs:
            conn.event_subs.discard(ws)

    async def fan_out_event(self, agent_id: str, payload: dict[str, Any]) -> None:
        conn = self._conns.get(agent_id)
        if not conn:
            return
        dead: list[WebSocket] = []
        for sub in list(conn.event_subs):
            try:
                await sub.send_text(json.dumps(payload))
            except Exception:
                dead.append(sub)
        for d in dead:
            conn.event_subs.discard(d)

    # ── Command dispatch ────────────────────────────────────

    async def dispatch(
        self,
        db: AsyncSession,
        agent_id: str,
        command: str,
        args: list[str],
        timeout_secs: int,
        issued_by: str,
    ) -> AgentCommand:
        row = AgentCommand(
            agent_id=agent_id,
            command=command,
            args_json=json.dumps(args),
            status="queued",
            issued_by=issued_by,
            timeout_secs=timeout_secs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        conn = self._conns.get(agent_id)
        if not conn:
            row.status = "failed"
            row.stderr = "agent not connected"
            row.completed_at = datetime.now(timezone.utc)
            await db.commit()
            logger.warning("dispatch_no_connection", agent_id=agent_id)
            return row

        payload = {
            "type": "exec",
            "command_id": row.id,
            "command": command,
            "args": args,
            "timeout_secs": timeout_secs,
        }
        try:
            await conn.send(payload)
        except Exception as e:
            row.status = "failed"
            row.stderr = f"send failed: {e}"
            row.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return row

        row.status = "dispatched"
        row.dispatched_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("command_dispatched", agent_id=agent_id, command_id=row.id, command=command)
        return row

    # ── Inbound result handling ─────────────────────────────

    async def handle_command_result(
        self,
        agent_id: str,
        command_id: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
        timed_out: bool,
    ) -> None:
        final_status: str | None = None
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    select(AgentCommand).where(AgentCommand.id == command_id)
                )
            ).scalar_one_or_none()
            if not row:
                logger.warning("command_result_orphan", command_id=command_id)
                return
            row.exit_code = exit_code
            row.stdout = stdout
            row.stderr = stderr
            row.duration_ms = duration_ms
            row.status = "timeout" if timed_out else (
                "completed" if exit_code == 0 else "failed"
            )
            final_status = row.status
            row.completed_at = datetime.now(timezone.utc)
            db.add(AgentEvent(
                agent_id=agent_id,
                event_type="command_finished",
                payload_json=json.dumps({
                    "command_id": command_id,
                    "exit_code": exit_code,
                    "status": row.status,
                    "duration_ms": duration_ms,
                }),
            ))
            await db.commit()
        await self.fan_out_event(agent_id, {
            "type": "command_finished",
            "command_id": command_id,
            "exit_code": exit_code,
            "status": final_status,
        })


# Singleton accessor
_dispatcher_singleton: CommandDispatcher | None = None


def get_dispatcher() -> CommandDispatcher:
    global _dispatcher_singleton
    if _dispatcher_singleton is None:
        _dispatcher_singleton = CommandDispatcher()
    return _dispatcher_singleton


__all__ = ["CommandDispatcher", "get_dispatcher"]
