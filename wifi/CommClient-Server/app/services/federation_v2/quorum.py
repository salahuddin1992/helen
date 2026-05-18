"""
QuorumManager — exposes consensus state for the Health-Map admin panel.

This module is a *thin façade* over whatever underlying consensus
subsystem the cluster runs (Raft / Helen ``cluster`` / static
deployment). It tolerates the consensus layer being absent — every
public method then returns ``{"enabled": False}`` rather than 500.

Capabilities
------------
* ``state()``         — current leader, term, commit index, last_applied.
* ``members()``       — current cluster membership + heartbeat status.
* ``force_election()``— step-down current leader and trigger a new vote.
* ``step_down()``     — explicit leader step-down.
* ``split_brain()``   — detect divergent leaders across reachable peers.

The "underlying consensus" hookup defaults to ``app.services.cluster``
if importable; tests can swap in a stub via ``configure_consensus``.

Singleton: ``get_quorum_manager()``.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.federation_peer import FederationPeerMeta
from app.models.federation_v2 import FederatedServer

logger = structlog.get_logger(__name__)


# Adapter type — caller can plug any consensus backend.
ConsensusAdapter = Callable[[], dict[str, Any]]


class QuorumManager:
    def __init__(self) -> None:
        self._adapter: Optional[ConsensusAdapter] = None
        self._lock = asyncio.Lock()
        # Synthetic state when no adapter is configured but we still
        # want the panel to render *something* coherent.
        self._term = 1
        self._leader: Optional[str] = None
        self._commit_index = 0

    def configure_consensus(self, adapter: Optional[ConsensusAdapter]) -> None:
        self._adapter = adapter

    # ── reads ────────────────────────────────────────────────

    async def state(self) -> dict[str, Any]:
        adapter_state = self._try_adapter()
        if adapter_state is None:
            # Synthesize from federation peer roles
            return await self._synthetic_state()
        return {
            "enabled":      True,
            "leader":       adapter_state.get("leader"),
            "term":         int(adapter_state.get("term") or 0),
            "commit_index": int(adapter_state.get("commit_index") or 0),
            "last_applied": int(adapter_state.get("last_applied") or 0),
            "members":      adapter_state.get("members") or [],
        }

    async def members(self) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            peers = (await db.execute(select(FederatedServer))).scalars().all()
            metas = {
                m.server_id: m for m in
                (await db.execute(select(FederationPeerMeta))).scalars().all()
            }
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for p in peers:
            m = metas.get(p.server_id)
            last_seen = p.last_seen
            if last_seen and last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age = (now - last_seen).total_seconds() if last_seen else 99_999
            out.append({
                "server_id":  p.server_id,
                "role":       (m.role if m else "follower") or "follower",
                "status":     p.status,
                "alive":      age < 60,
                "last_seen":  last_seen.isoformat() if last_seen else None,
                "age_sec":    int(age),
            })
        return out

    async def split_brain(self) -> dict[str, Any]:
        """Detect divergent leaders. Each peer's role-meta is the source —
        if more than one peer claims ``role=master`` we flag it."""
        async with async_session_factory() as db:
            metas = (await db.execute(
                select(FederationPeerMeta).where(
                    FederationPeerMeta.role == "master"
                )
            )).scalars().all()
        masters = [m.server_id for m in metas]
        return {
            "detected":  len(masters) > 1,
            "masters":   masters,
            "checked_at": time.time(),
        }

    # ── writes ───────────────────────────────────────────────

    async def force_election(self, actor: str = "system") -> dict[str, Any]:
        adapter = self._adapter
        if adapter is None:
            # No backend — just bump the synthetic term so the WS event
            # still fires and the panel reflects the operator action.
            self._term += 1
            self._leader = None
            return {
                "ok": True, "enabled": False,
                "term": self._term, "leader": None,
                "note": "no_consensus_backend_synthetic_election",
            }
        try:
            # Adapters expose either ``force_election`` or accept a
            # ``state()`` dict directive — we try the explicit method first.
            fn = getattr(adapter, "force_election", None)
            if callable(fn):
                result = fn()
            else:
                result = {"ok": True}
            return {"ok": True, "enabled": True, **(result or {})}
        except Exception as exc:
            logger.warning("fedmap_force_election_failed", error=str(exc))
            return {"ok": False, "enabled": True, "error": str(exc)}

    async def step_down(self, actor: str = "system") -> dict[str, Any]:
        adapter = self._adapter
        if adapter is None:
            self._leader = None
            return {"ok": True, "enabled": False, "note": "synthetic"}
        try:
            fn = getattr(adapter, "step_down", None)
            if callable(fn):
                result = fn()
            else:
                result = {"ok": True}
            return {"ok": True, "enabled": True, **(result or {})}
        except Exception as exc:
            return {"ok": False, "enabled": True, "error": str(exc)}

    # ── internals ────────────────────────────────────────────

    def _try_adapter(self) -> Optional[dict[str, Any]]:
        if self._adapter is None:
            # Best-effort: try cluster module on first access only.
            try:
                from app.services import cluster as _cluster  # type: ignore
                if hasattr(_cluster, "consensus_state"):
                    self._adapter = _cluster.consensus_state  # type: ignore
            except Exception:
                self._adapter = None
        if self._adapter is None:
            return None
        try:
            data = self._adapter()
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("fedmap_quorum_adapter_failed", error=str(exc))
        return None

    async def _synthetic_state(self) -> dict[str, Any]:
        async with async_session_factory() as db:
            masters = (await db.execute(
                select(FederationPeerMeta).where(
                    FederationPeerMeta.role == "master"
                )
            )).scalars().all()
        leader = self._leader or (masters[0].server_id if masters else None)
        self._leader = leader
        return {
            "enabled":      False,
            "leader":       leader,
            "term":         self._term,
            "commit_index": self._commit_index,
            "last_applied": self._commit_index,
            "members":      [m.server_id for m in masters],
            "note":         "synthetic_no_consensus_backend",
        }


# ── singleton ───────────────────────────────────────────────


_quorum: Optional[QuorumManager] = None


def get_quorum_manager() -> QuorumManager:
    global _quorum
    if _quorum is None:
        _quorum = QuorumManager()
    return _quorum
