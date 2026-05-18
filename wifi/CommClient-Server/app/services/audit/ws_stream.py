"""
Audit WebSocket fan-out.

Maintains a set of active WebSocket subscribers and broadcasts:
    * new audit entries (via the chain pub-sub hook)
    * alerts derived from real-time rule evaluation

The manager is process-local. Cross-node fan-out is handled by the
audit replication pipeline (``app.services.audit_replication``) — each
node runs its own ``AuditWebSocketManager`` and replication causes
each node to ``append`` the entry locally, which feeds its local
subscribers.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.services.audit.alert_rules import (
    CompiledRule,
    get_engine as get_rules_engine,
)
from app.services.audit.chain import AuditEntry, subscribe

logger = get_logger(__name__)


class _Subscriber:
    """Lightweight wrapper that owns one outbound WebSocket. We don't
    import the FastAPI ``WebSocket`` type here to keep the module
    importable in headless contexts (tests)."""
    __slots__ = ("ws", "filters", "queue", "alive")

    def __init__(self, ws: Any, filters: dict[str, Any]) -> None:
        self.ws = ws
        self.filters = filters
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.alive = True


class AuditWebSocketManager:
    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._registered = False

    def _maybe_register_hook(self) -> None:
        if self._registered:
            return
        subscribe(self._on_entry)
        self._registered = True

    async def connect(self, ws: Any, filters: Optional[dict[str, Any]] = None) -> _Subscriber:
        await ws.accept()
        sub = _Subscriber(ws, filters or {})
        async with self._lock:
            self._subs.add(sub)
            self._maybe_register_hook()
        logger.info("audit_ws_connected", n_subs=len(self._subs))
        return sub

    async def disconnect(self, sub: _Subscriber) -> None:
        sub.alive = False
        async with self._lock:
            self._subs.discard(sub)
        try:
            await sub.ws.close()
        except Exception:
            pass
        logger.info("audit_ws_disconnected", n_subs=len(self._subs))

    def _on_entry(self, entry: AuditEntry) -> None:
        """Synchronous hook called by chain.publish — enqueue the
        serialised event for every interested subscriber."""
        try:
            payload = {
                "type": "entry",
                "entry": {
                    "seq": entry.seq,
                    "timestamp": entry.timestamp,
                    "actor": entry.actor,
                    "action": entry.action,
                    "resource": entry.target,
                    "payload": entry.payload,
                    "payload_hash": entry.payload_hash,
                    "prev_hash": entry.prev_hash,
                    "chain_hash": entry.chain_hash,
                },
                "ts": datetime.now(timezone.utc).isoformat(),
            }

            for sub in list(self._subs):
                if not sub.alive:
                    continue
                if not self._matches_filters(entry, sub.filters):
                    continue
                try:
                    sub.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.warning("audit_ws_queue_full")
                    sub.alive = False

            # Real-time alerts via the rules engine
            try:
                engine = get_rules_engine()
                matched: list[CompiledRule] = engine.evaluate(entry)
                for rule in matched:
                    alert = {
                        "type": "alert",
                        "rule_id": rule.id,
                        "rule_name": rule.name,
                        "severity": rule.severity,
                        "channels": rule.channels,
                        "entry_seq": entry.seq,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    for sub in list(self._subs):
                        if sub.alive:
                            try:
                                sub.queue.put_nowait(alert)
                            except asyncio.QueueFull:
                                sub.alive = False
            except Exception as exc:
                logger.debug("audit_ws_rule_eval_failed", error=str(exc))
        except Exception as exc:
            logger.warning("audit_ws_fanout_failed", error=str(exc))

    @staticmethod
    def _matches_filters(entry: AuditEntry, filters: dict[str, Any]) -> bool:
        if not filters:
            return True
        if filters.get("actor") and entry.actor != filters["actor"]:
            return False
        if filters.get("action") and entry.action != filters["action"]:
            return False
        if filters.get("resource") and entry.target != filters["resource"]:
            return False
        return True

    async def pump(self, sub: _Subscriber) -> None:
        """Drain the per-subscriber queue into the WebSocket. Returns
        when the socket closes or the subscriber is marked dead."""
        try:
            while sub.alive:
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=30)
                    await sub.ws.send_text(json.dumps(msg, default=str))
                except asyncio.TimeoutError:
                    # heartbeat
                    await sub.ws.send_text(json.dumps({
                        "type": "ping",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }))
                except Exception:
                    break
        finally:
            await self.disconnect(sub)

    def stats(self) -> dict[str, Any]:
        return {
            "subscribers": len(self._subs),
            "registered_hook": self._registered,
        }


_manager: Optional[AuditWebSocketManager] = None


def get_ws_manager() -> AuditWebSocketManager:
    global _manager
    if _manager is None:
        _manager = AuditWebSocketManager()
    return _manager


__all__ = ["AuditWebSocketManager", "get_ws_manager"]
