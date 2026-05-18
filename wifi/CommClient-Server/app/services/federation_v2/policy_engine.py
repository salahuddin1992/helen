"""
FederationPolicyEngine — deterministic routing policy evaluator.

A policy describes a match expression and an action. Rules are
ordered by ``priority`` (ascending) and the first matching one wins.

Match grammar (JSON)
--------------------
::

    {
      "kind":        "message" | "edit" | ...,
      "channel":     "<exact>" | {"regex": "..."},
      "sender":      "<exact>" | {"regex": "..."},
      "origin":      "<server_id>",
      "region":      "us-east",
      "min_trust":   "trusted" | "peer" | "restricted",
      "max_rtt_ms":  500,
      "any":         [ <expr>, <expr>, ... ],
      "all":         [ <expr>, <expr>, ... ]
    }

Action grammar (JSON)
---------------------
::

    {
      "route_to":   ["server-a", "server-b"],
      "fallback":   ["server-c"],
      "blackhole":  false,
      "require_trust": "peer",
      "tag":        "high-priority"
    }

Singleton: ``get_policy_engine()``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.federation_peer import FederationPeerMeta
from app.models.federation_policy import FederationPolicy
from app.models.federation_v2 import FederatedServer

logger = structlog.get_logger(__name__)


TRUST_RANK = {"trusted": 3, "peer": 2, "restricted": 1, "untrusted": 0}


@dataclass
class PolicyDecision:
    matched_policy_id: Optional[str]
    matched_policy_name: Optional[str]
    route_to: list[str]
    fallback: list[str]
    blackhole: bool
    tag: Optional[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_policy_id":   self.matched_policy_id,
            "matched_policy_name": self.matched_policy_name,
            "route_to":            self.route_to,
            "fallback":            self.fallback,
            "blackhole":           self.blackhole,
            "tag":                 self.tag,
            "reason":              self.reason,
        }


class FederationPolicyEngine:
    # ── CRUD ─────────────────────────────────────────────────

    async def list_policies(self) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(FederationPolicy)
                .order_by(FederationPolicy.priority.asc())
            )).scalars().all()
        return [self._to_dict(r) for r in rows]

    async def create_policy(
        self,
        *,
        name: str,
        match: dict[str, Any],
        action: dict[str, Any],
        priority: int = 100,
        description: str = "",
        enabled: bool = True,
        actor: str = "system",
    ) -> dict[str, Any]:
        async with async_session_factory() as db:
            row = FederationPolicy(
                name=name,
                description=description,
                priority=int(priority),
                enabled=bool(enabled),
                match=match or {},
                action=action or {},
                created_by=actor,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return self._to_dict(row)

    async def delete_policy(self, policy_id: str) -> bool:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederationPolicy).where(FederationPolicy.id == policy_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
        return True

    # ── routing ──────────────────────────────────────────────

    async def route(self, envelope: dict[str, Any]) -> PolicyDecision:
        return await self._evaluate(envelope, rules_override=None)

    async def simulate(
        self,
        envelope: dict[str, Any],
        rules_override: Optional[list[dict[str, Any]]] = None,
    ) -> PolicyDecision:
        return await self._evaluate(envelope, rules_override=rules_override)

    # ── internals ────────────────────────────────────────────

    async def _evaluate(
        self,
        envelope: dict[str, Any],
        rules_override: Optional[list[dict[str, Any]]],
    ) -> PolicyDecision:
        peers, metas = await self._snapshot()
        rules = rules_override
        if rules is None:
            async with async_session_factory() as db:
                rows = (await db.execute(
                    select(FederationPolicy)
                    .where(FederationPolicy.enabled.is_(True))
                    .order_by(FederationPolicy.priority.asc())
                )).scalars().all()
            rules = [self._to_dict(r) for r in rows]

        for rule in rules:
            if self._match(rule.get("match") or {}, envelope, peers, metas):
                action = rule.get("action") or {}
                # Filter route_to by trust if requested
                route_to = list(action.get("route_to") or [])
                min_trust = action.get("require_trust")
                if min_trust:
                    rank = TRUST_RANK.get(min_trust, 0)
                    allowed = {
                        p.server_id for p in peers
                        if TRUST_RANK.get(p.trust_level, 0) >= rank
                    }
                    route_to = [s for s in route_to if s in allowed]
                return PolicyDecision(
                    matched_policy_id=rule.get("id"),
                    matched_policy_name=rule.get("name"),
                    route_to=route_to,
                    fallback=list(action.get("fallback") or []),
                    blackhole=bool(action.get("blackhole")),
                    tag=action.get("tag"),
                    reason="match",
                )

        # Default: broadcast to all healthy peers.
        default_targets = [
            p.server_id for p in peers
            if p.status == "active"
        ]
        return PolicyDecision(
            matched_policy_id=None,
            matched_policy_name=None,
            route_to=default_targets,
            fallback=[],
            blackhole=False,
            tag="default",
            reason="default_broadcast",
        )

    def _match(
        self,
        match: dict[str, Any],
        envelope: dict[str, Any],
        peers: list[FederatedServer],
        metas: dict[str, FederationPeerMeta],
    ) -> bool:
        if not match:
            return True

        # Composite predicates
        if "all" in match:
            return all(self._match(sub, envelope, peers, metas) for sub in match["all"])
        if "any" in match:
            return any(self._match(sub, envelope, peers, metas) for sub in match["any"])

        def _eq_or_regex(spec: Any, value: str) -> bool:
            if spec is None:
                return True
            if isinstance(spec, str):
                return spec == value
            if isinstance(spec, dict):
                pat = spec.get("regex")
                if pat:
                    try:
                        return bool(re.search(pat, value or ""))
                    except re.error:
                        return False
            return False

        if "kind" in match and not _eq_or_regex(match["kind"], str(envelope.get("kind") or envelope.get("type") or "")):
            return False
        if "channel" in match and not _eq_or_regex(match["channel"], str(envelope.get("channel") or "")):
            return False
        if "sender" in match and not _eq_or_regex(match["sender"], str(envelope.get("sender") or "")):
            return False
        if "origin" in match and not _eq_or_regex(match["origin"], str(envelope.get("origin") or "")):
            return False

        if "region" in match:
            origin = str(envelope.get("origin") or "")
            meta = metas.get(origin)
            if meta is None or (meta.region or "") != match["region"]:
                return False

        if "min_trust" in match:
            origin = str(envelope.get("origin") or "")
            rank = TRUST_RANK.get(match["min_trust"], 0)
            peer = next((p for p in peers if p.server_id == origin), None)
            if peer is None or TRUST_RANK.get(peer.trust_level, 0) < rank:
                return False

        if "max_rtt_ms" in match:
            origin = str(envelope.get("origin") or "")
            meta = metas.get(origin)
            rtt = float(meta.last_rtt_ms) if meta else 0.0
            if rtt > float(match["max_rtt_ms"]):
                return False

        return True

    async def _snapshot(self) -> tuple[list[FederatedServer], dict[str, FederationPeerMeta]]:
        async with async_session_factory() as db:
            peers = (await db.execute(select(FederatedServer))).scalars().all()
            metas = (await db.execute(select(FederationPeerMeta))).scalars().all()
        return list(peers), {m.server_id: m for m in metas}

    def _to_dict(self, row: FederationPolicy) -> dict[str, Any]:
        return {
            "id":          row.id,
            "name":        row.name,
            "description": row.description,
            "priority":    row.priority,
            "enabled":     row.enabled,
            "match":       row.match or {},
            "action":      row.action or {},
            "created_by":  row.created_by,
            "created_at":  row.created_at.isoformat() if row.created_at else None,
        }


# ── singleton ───────────────────────────────────────────────


_engine: Optional[FederationPolicyEngine] = None


def get_policy_engine() -> FederationPolicyEngine:
    global _engine
    if _engine is None:
        _engine = FederationPolicyEngine()
    return _engine
