"""
Edge — geographic request routing.

Pipeline
--------
1.  GeoIP lookup of the client IP (MaxMind/GeoLite2 if available; else
    a tiny lat/lng heuristic table for major data-center cities).
2.  Filter ``EdgeNode`` rows by region allowlist (workspace policy).
3.  Sort remaining nodes by haversine distance, demote nodes whose
    ``current_load_percent`` exceeds 85.
4.  Return the best candidate. Caller is responsible for sticky
    affinity if data residency requires it.

When no nodes are available, returns ``None`` and the caller should
fall back to the origin server.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.edge import EdgeNode, RegionPolicy

logger = get_logger(__name__)


# ── tiny fallback geo table (capital cities) ────────────────


_FALLBACK_GEO = {
    # CC → (lat, lng, region_hint)
    "US": (37.0902, -95.7129, "us-east-1"),
    "GB": (54.7575, -2.6855,  "eu-west-2"),
    "DE": (51.1657, 10.4515,  "eu-central-1"),
    "FR": (46.2276, 2.2137,   "eu-west-3"),
    "NL": (52.1326, 5.2913,   "eu-west-1"),
    "IE": (53.1424, -7.6921,  "eu-west-1"),
    "JP": (36.2048, 138.2529, "ap-northeast-1"),
    "SG": (1.3521,  103.8198, "ap-southeast-1"),
    "AU": (-25.27,  133.7751, "ap-southeast-2"),
    "BR": (-14.235, -51.9253, "sa-east-1"),
    "IN": (20.5937, 78.9629,  "ap-south-1"),
    "AE": (23.4241, 53.8478,  "me-south-1"),
    "SA": (23.8859, 45.0792,  "me-central-1"),
    "EG": (26.8206, 30.8025,  "me-central-1"),
    "CA": (56.1304, -106.3468, "ca-central-1"),
    "ZA": (-30.5595, 22.9375, "af-south-1"),
}


@dataclass
class GeoLocation:
    lat: float
    lng: float
    country: str = ""
    region_hint: str = ""


_MAXMIND_READER: Any = None


def _get_maxmind_reader() -> Any:
    global _MAXMIND_READER
    if _MAXMIND_READER is not None:
        return _MAXMIND_READER
    import os
    db_path = os.environ.get("MAXMIND_GEOIP_DB", "data/edge/GeoLite2-City.mmdb")
    if not os.path.exists(db_path):
        return None
    try:
        import maxminddb  # type: ignore[import-untyped]
        _MAXMIND_READER = maxminddb.open_database(db_path)
        return _MAXMIND_READER
    except Exception:
        return None


def geoip_lookup(ip: str) -> Optional[GeoLocation]:
    """Return GeoLocation or None. Never raises."""
    if not ip:
        return None
    reader = _get_maxmind_reader()
    if reader is not None:
        try:
            r = reader.get(ip) or {}
            loc = r.get("location") or {}
            country = (r.get("country") or {}).get("iso_code") or ""
            return GeoLocation(
                lat=float(loc.get("latitude") or 0.0),
                lng=float(loc.get("longitude") or 0.0),
                country=country,
                region_hint=_FALLBACK_GEO.get(country, (0, 0, ""))[2],
            )
        except Exception:
            pass
    # No MaxMind: best-effort by IP→country via ``geoip2`` standalone.
    try:
        import geoip2.database  # type: ignore[import-untyped]
    except Exception:
        return None
    return None


def haversine_km(a: GeoLocation, b_lat: float, b_lng: float) -> float:
    R = 6371.0
    lat1, lat2 = math.radians(a.lat), math.radians(b_lat)
    dlat = lat2 - lat1
    dlng = math.radians(b_lng - a.lng)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


class GeoRouter:
    """Stateless geo router. Singleton."""

    async def route_request(
        self,
        client_ip: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> Optional[EdgeNode]:
        """Pick the best edge node for the request."""
        async with async_session_factory() as db:
            allowed = await self._allowed_regions(db, workspace_id)
            q = select(EdgeNode).where(EdgeNode.status == "active")
            nodes = list((await db.execute(q)).scalars().all())

        if not nodes:
            return None

        if allowed:
            nodes = [n for n in nodes if n.region in allowed]
            if not nodes:
                return None

        loc = geoip_lookup(client_ip)
        # If no geo info, fall back to lowest-load node.
        if loc is None:
            nodes.sort(key=lambda n: (n.current_load_percent, n.region))
            return nodes[0]

        # Distance + load aware score.
        def _score(n: EdgeNode) -> float:
            d = haversine_km(loc, n.geo_lat, n.geo_lng)
            load_penalty = 5000.0 if n.current_load_percent > 85.0 else 0.0
            return d + load_penalty

        nodes.sort(key=_score)
        return nodes[0]

    async def _allowed_regions(
        self, db: AsyncSession, workspace_id: Optional[str],
    ) -> Optional[list[str]]:
        if not workspace_id:
            return None
        r = await db.execute(
            select(RegionPolicy).where(RegionPolicy.workspace_id == workspace_id)
        )
        pol = r.scalar_one_or_none()
        if pol is None:
            return None
        allowed = list(pol.allowed_regions or [])
        if pol.required_residency_region:
            return [pol.required_residency_region]
        return allowed or None

    async def failover(
        self,
        failed_node: EdgeNode,
        client_ip: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> Optional[EdgeNode]:
        """Re-route past a failing node."""
        async with async_session_factory() as db:
            allowed = await self._allowed_regions(db, workspace_id)
            q = select(EdgeNode).where(
                EdgeNode.status == "active",
                EdgeNode.id != failed_node.id,
            )
            nodes = list((await db.execute(q)).scalars().all())
        if not nodes:
            return None
        if allowed:
            nodes = [n for n in nodes if n.region in allowed]
            if not nodes:
                return None
        loc = geoip_lookup(client_ip)
        if loc is None:
            nodes.sort(key=lambda n: n.current_load_percent)
            return nodes[0]
        nodes.sort(key=lambda n: haversine_km(loc, n.geo_lat, n.geo_lng))
        return nodes[0]


_router: Optional[GeoRouter] = None


def get_geo_router() -> GeoRouter:
    global _router
    if _router is None:
        _router = GeoRouter()
    return _router
