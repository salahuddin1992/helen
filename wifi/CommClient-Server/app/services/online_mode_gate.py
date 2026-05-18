"""
Online-Mode master gate — single, persistent, off-by-default switch
that controls every Helen feature that *can* talk to the internet.

Why
---
Helen's hard rule is "100% LAN-only by default" (see CLAUDE.md hard
rule #1). Group 2 added a handful of features that *can* legitimately
reach the public internet — UPnP port-mapping, DNS upstream
forwarding, TURN external probes, etc. — but none of them should
ever activate silently. The operator must opt in.

This module is the single source of truth for that opt-in. Each
online-capable feature registers itself here at startup with two
callbacks (``start`` and ``stop``). The gate persists its on/off
state to ``data/online_mode.json`` and:

  * **Off (default)** — every registered service stays dormant.
    Helen runs as a pure-LAN deployment.
  * **On** — every registered service is started in the order it
    was registered. Flipping back off stops them in reverse order.

Persistence + audit
-------------------
State is saved to ``$DATA_DIR/online_mode.json`` so the gate's
position survives restarts. Every flip emits a structured
``online_mode_changed`` log line and (when available) appends to
the tamper-evident audit chain.

Concurrency
-----------
A single ``asyncio.Lock`` serializes flips so concurrent admin
clicks can't end up with half-started services.

Wire shape
----------
    gate = get_online_mode_gate()
    gate.register("wan_portmap", start=start_wan, stop=stop_wan)
    gate.register("dns_upstream", start=enable_fwd, stop=disable_fwd)

    # Lifespan loads persisted state and brings up registered
    # services if it was previously enabled:
    await gate.bootstrap()

    # Admin endpoint flips it:
    await gate.enable(reason="admin click", actor="user-42")

Dedicated env (no edits to existing modules required)
-----------------------------------------------------
    HELEN_ONLINE_MODE_DEFAULT=on    Override the default-off behavior
                                      (only matters on first boot when
                                      the persisted file is absent).
    HELEN_ONLINE_MODE_PATH=...       Override storage path for the
                                      persisted state file.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from app.core.logging import get_logger

logger = get_logger(__name__)


StartFn = Callable[[], Union[None, Awaitable[None]]]
StopFn = Callable[[], Union[None, Awaitable[None]]]


@dataclass
class _RegisteredService:
    name: str
    start: StartFn
    stop: StopFn
    is_running: bool = False
    last_error: Optional[str] = None
    last_started_at: Optional[float] = None
    last_stopped_at: Optional[float] = None


@dataclass
class GateHistoryEntry:
    at: float
    enabled: bool
    actor: Optional[str]
    reason: Optional[str]


class OnlineModeGate:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._enabled: bool = False
        self._lock = asyncio.Lock()
        self._services: list[_RegisteredService] = []
        self._history: list[GateHistoryEntry] = []
        self._last_change_at: Optional[float] = None
        self._last_actor: Optional[str] = None
        self._last_reason: Optional[str] = None

    # ── Persistence ──────────────────────────────────────────

    def _load_state(self) -> None:
        if not self.state_path.exists():
            # First boot: respect the env-driven default.
            default = os.environ.get(
                "HELEN_ONLINE_MODE_DEFAULT", "off",
            ).strip().lower()
            self._enabled = default in ("1", "on", "true", "yes")
            return
        try:
            payload = json.loads(self.state_path.read_text("utf-8"))
            self._enabled = bool(payload.get("enabled", False))
            self._last_change_at = payload.get("last_change_at")
            self._last_actor = payload.get("last_actor")
            self._last_reason = payload.get("last_reason")
        except Exception as e:
            logger.warning("online_mode_state_load_failed",
                           error=str(e), path=str(self.state_path))
            self._enabled = False

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "enabled": self._enabled,
                "last_change_at": self._last_change_at,
                "last_actor": self._last_actor,
                "last_reason": self._last_reason,
                "saved_at": time.time(),
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.state_path)
        except Exception as e:
            logger.warning("online_mode_state_save_failed",
                           error=str(e), path=str(self.state_path))

    # ── Registration ─────────────────────────────────────────

    def register(self, name: str, *,
                 start: StartFn, stop: StopFn) -> None:
        """Register a feature that should start when the gate flips
        on and stop when it flips off. Idempotent on ``name`` — a
        second register call replaces the first."""
        existing = next((s for s in self._services if s.name == name),
                        None)
        if existing:
            existing.start = start
            existing.stop = stop
            return
        self._services.append(_RegisteredService(
            name=name, start=start, stop=stop,
        ))

    def unregister(self, name: str) -> None:
        self._services = [s for s in self._services if s.name != name]

    # ── State queries ────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "last_change_at": self._last_change_at,
            "last_actor": self._last_actor,
            "last_reason": self._last_reason,
            "state_path": str(self.state_path),
            "services": [
                {
                    "name": s.name,
                    "running": s.is_running,
                    "last_started_at": s.last_started_at,
                    "last_stopped_at": s.last_stopped_at,
                    "last_error": s.last_error,
                }
                for s in self._services
            ],
            "history": [
                {"at": h.at, "enabled": h.enabled,
                 "actor": h.actor, "reason": h.reason}
                for h in self._history[-25:]
            ],
        }

    # ── Lifecycle ────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Called once from the lifespan startup. Loads persisted
        state and, if the gate was last left ON, brings every
        registered service up."""
        self._load_state()
        if self._enabled:
            logger.info("online_mode_bootstrap_resume_on")
            await self._start_all()
        else:
            logger.info("online_mode_bootstrap_off")

    async def shutdown(self) -> None:
        """Called from the lifespan shutdown. Stops services without
        flipping the persisted state — when Helen comes back up the
        gate will resume to whatever it was."""
        await self._stop_all()

    async def enable(self, *, actor: Optional[str] = None,
                       reason: Optional[str] = None) -> dict:
        async with self._lock:
            if self._enabled:
                return self.status()
            self._enabled = True
            self._last_change_at = time.time()
            self._last_actor = actor
            self._last_reason = reason
            self._history.append(GateHistoryEntry(
                at=self._last_change_at, enabled=True,
                actor=actor, reason=reason,
            ))
            self._save_state()
            await self._audit("online_mode_enabled", actor, reason)
            await self._start_all()
            return self.status()

    async def disable(self, *, actor: Optional[str] = None,
                        reason: Optional[str] = None) -> dict:
        async with self._lock:
            if not self._enabled:
                return self.status()
            self._enabled = False
            self._last_change_at = time.time()
            self._last_actor = actor
            self._last_reason = reason
            self._history.append(GateHistoryEntry(
                at=self._last_change_at, enabled=False,
                actor=actor, reason=reason,
            ))
            self._save_state()
            await self._audit("online_mode_disabled", actor, reason)
            await self._stop_all()
            return self.status()

    # ── Internals ────────────────────────────────────────────

    async def _start_all(self) -> None:
        for s in self._services:
            if s.is_running:
                continue
            try:
                result = s.start()
                if asyncio.iscoroutine(result):
                    await result
                s.is_running = True
                s.last_started_at = time.time()
                s.last_error = None
                logger.info("online_mode_service_started", service=s.name)
            except Exception as e:
                s.last_error = str(e)
                logger.warning("online_mode_service_start_failed",
                               service=s.name, error=str(e))

    async def _stop_all(self) -> None:
        # Reverse order so a "later" service that depends on an
        # "earlier" one is torn down first.
        for s in reversed(self._services):
            if not s.is_running:
                continue
            try:
                result = s.stop()
                if asyncio.iscoroutine(result):
                    await result
                s.is_running = False
                s.last_stopped_at = time.time()
                logger.info("online_mode_service_stopped", service=s.name)
            except Exception as e:
                s.last_error = str(e)
                logger.warning("online_mode_service_stop_failed",
                               service=s.name, error=str(e))

    async def _audit(self, event: str, actor: Optional[str],
                       reason: Optional[str]) -> None:
        try:
            from app.core.audit import audit_log
            audit_log(
                event,
                actor=actor or "system",
                metadata={"reason": reason or ""},
            )
        except Exception:
            # Audit chain may not be wired in tests — never let
            # logging failures block a state transition.
            pass


# ── Singleton helpers ────────────────────────────────────────────


_gate: Optional[OnlineModeGate] = None


def configure_online_mode_gate(state_path: Optional[Path] = None
                                  ) -> OnlineModeGate:
    """Build (or replace) the process-wide gate. Idempotent: callers
    who configure twice get the second instance — handy in tests."""
    global _gate
    if state_path is None:
        env_path = os.environ.get("HELEN_ONLINE_MODE_PATH")
        if env_path:
            state_path = Path(env_path)
        else:
            try:
                from app.core.config import get_settings
                state_path = (get_settings().PROJECT_ROOT
                                / "data" / "online_mode.json")
            except Exception:
                state_path = Path("data/online_mode.json")
    _gate = OnlineModeGate(state_path)
    return _gate


def get_online_mode_gate() -> Optional[OnlineModeGate]:
    return _gate


def reset_online_mode_gate() -> None:
    global _gate
    _gate = None


__all__ = [
    "OnlineModeGate",
    "GateHistoryEntry",
    "configure_online_mode_gate",
    "get_online_mode_gate",
    "reset_online_mode_gate",
]
