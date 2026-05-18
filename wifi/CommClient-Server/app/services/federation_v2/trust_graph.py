"""
Federation v2 — web of trust.

A directed graph keyed on ``server_id``. Edges are
``FederationTrustToken`` rows.  ``trust_score(server)`` reflects the
walk-derived reputation, blended with the operator's hard
``trust_level``:

    trusted     → +1.0   (cap)
    peer        →  base
    restricted  → -0.4
    banned      →  0.0   (hard floor)

The default ``base`` is 0.5. The trust graph BFS sums signed
contributions from neighbouring trusted servers, decaying by 0.5 per
hop, clipped to [0.0, 1.0].
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.federation_v2 import FederatedServer, FederationTrustToken

logger = get_logger(__name__)


HARD_FLOOR = {"banned": 0.0, "suspended": 0.1}
LEVEL_BASE = {
    "trusted":    0.95,
    "peer":       0.5,
    "restricted": 0.2,
    "untrusted":  0.05,
}
DEFAULT_BLOCKLIST_FILE = "data/fedv2/blocklist.json"
DEFAULT_ALLOWLIST_FILE = "data/fedv2/allowlist.json"


class TrustGraph:
    """In-memory cached trust graph."""

    def __init__(self) -> None:
        self._edges: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
        # server → [(neighbour, weight, expires_ts)]
        self._loaded_at: float = 0.0
        self._allowlist: set[str] = set()
        self._blocklist: set[str] = set()

    async def reload(self) -> None:
        async with async_session_factory() as db:
            servers = (await db.execute(select(FederatedServer))).scalars().all()
            tokens = (await db.execute(select(FederationTrustToken))).scalars().all()
        edges: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
        now = time.time()
        for t in tokens:
            if t.revoked:
                continue
            try:
                exp = t.expires_at.timestamp()
            except Exception:
                exp = now + 86400
            if exp < now:
                continue
            # Weight by scope: "trusted" scope > "peer" scope.
            weight = {
                "trusted":    1.0,
                "peer":       0.6,
                "restricted": 0.2,
            }.get(t.scope, 0.5)
            edges[t.issuing_server].append(
                (t.subject_server, weight, exp),
            )
        self._edges = edges
        self._loaded_at = now
        # Seed allowlist/blocklist from server status
        self._allowlist = {s.server_id for s in servers if s.trust_level == "trusted"}
        self._blocklist = {s.server_id for s in servers if s.status == "banned"}
        self._load_static_lists()

    def _load_static_lists(self) -> None:
        import json
        import os
        for path, target in ((DEFAULT_BLOCKLIST_FILE, self._blocklist),
                              (DEFAULT_ALLOWLIST_FILE, self._allowlist)):
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        for s in json.load(f) or []:
                            target.add(str(s).lower())
            except Exception:
                pass

    async def trust_score(self, server_id: str) -> float:
        """BFS-based trust scoring."""
        from app.services.federation_v2.addressing import my_server_id
        if not self._edges:
            await self.reload()
        if server_id in self._blocklist:
            return 0.0
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederatedServer).where(
                    FederatedServer.server_id == server_id
                )
            )).scalar_one_or_none()
        if row is None:
            return 0.0
        if row.status in HARD_FLOOR:
            return HARD_FLOOR[row.status]
        base = LEVEL_BASE.get(row.trust_level, 0.5)
        # BFS contribution from local-trust roots (we treat self as full trust).
        root = my_server_id()
        visited: dict[str, float] = {root: 1.0}
        q: deque[tuple[str, float, int]] = deque([(root, 1.0, 0)])
        contributions = 0.0
        while q:
            s, score, hops = q.popleft()
            if hops > 4:
                continue
            for nbr, w, _exp in self._edges.get(s, []):
                new_score = score * w * 0.5
                if nbr in visited and visited[nbr] >= new_score:
                    continue
                visited[nbr] = new_score
                if nbr == server_id:
                    contributions += new_score
                q.append((nbr, new_score, hops + 1))
        out = max(0.0, min(1.0, base + 0.5 * contributions))
        return out

    async def is_allowed(self, server_id: str) -> bool:
        if server_id in self._blocklist:
            return False
        if not self._allowlist:
            return True
        # Allowlist mode: only listed servers.
        return server_id in self._allowlist

    def add_to_blocklist(self, server_id: str) -> None:
        self._blocklist.add(server_id)

    def add_to_allowlist(self, server_id: str) -> None:
        self._allowlist.add(server_id)

    async def export_graph(self) -> dict[str, Any]:
        """Snapshot for the admin UI."""
        await self.reload()
        async with async_session_factory() as db:
            servers = (await db.execute(select(FederatedServer))).scalars().all()
        nodes = []
        for s in servers:
            nodes.append({
                "server_id":    s.server_id,
                "trust_level":  s.trust_level,
                "status":       s.status,
                "trust_score":  s.trust_score,
            })
        edges = []
        for issuer, edge_list in self._edges.items():
            for subject, weight, exp in edge_list:
                edges.append({
                    "from": issuer, "to": subject,
                    "weight": weight, "expires_at": exp,
                })
        return {
            "nodes": nodes, "edges": edges,
            "blocklist": sorted(self._blocklist),
            "allowlist": sorted(self._allowlist),
        }


_graph: Optional[TrustGraph] = None


def get_trust_graph() -> TrustGraph:
    global _graph
    if _graph is None:
        _graph = TrustGraph()
    return _graph
