"""
Marketplace client.

Pulls plugin manifests from one or more remote marketplace endpoints and
keeps a local cache. Defaults to Helen's first-party marketplace; admins
can extend the URL list via env or admin API.

Marketplace JSON contract (each remote returns):

    {
      "plugins": [
        {
          "manifest": { ...Manifest v1... },
          "category": "comms",
          "rating_avg": 4.6,
          "ratings_count": 132,
          "downloads": 9281,
          "screenshots": ["https://..."],
          "long_description": "markdown..."
        },
        ...
      ]
    }
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.plugin import MarketplaceListing, PluginManifest
from app.services.plugins.loader import register_manifest

logger = get_logger(__name__)


DEFAULT_MARKETPLACES = [
    "https://marketplace.helen.app/v1/plugins.json",
]


def _configured_marketplaces() -> list[str]:
    extra = os.getenv("HELEN_PLUGIN_MARKETPLACES", "")
    out = list(DEFAULT_MARKETPLACES)
    if extra:
        out.extend(u.strip() for u in extra.split(",") if u.strip())
    return out


# ───────────────────────────────────────────────────────────────────────
# Cache
# ───────────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    fetched_at: float
    payload: dict[str, Any]


_CACHE_TTL_SEC = 300
_cache: dict[str, _CacheEntry] = {}


def _fetch_one(url: str) -> dict[str, Any]:
    entry = _cache.get(url)
    if entry and (time.time() - entry.fetched_at < _CACHE_TTL_SEC):
        return entry.payload
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read(8 * 1024 * 1024).decode("utf-8"))
        _cache[url] = _CacheEntry(time.time(), data)
        return data
    except Exception as e:                                              # noqa: BLE001
        logger.warning("marketplace.fetch %s: %s", url, e)
        return {"plugins": [], "_error": str(e)}


def fetch_all() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in _configured_marketplaces():
        data = _fetch_one(url)
        for p in data.get("plugins", []):
            slug = (p.get("manifest") or {}).get("slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append(p)
    return out


# ───────────────────────────────────────────────────────────────────────
# Sync to DB
# ───────────────────────────────────────────────────────────────────────


async def sync_marketplace_to_db(db: AsyncSession) -> dict[str, int]:
    """Register every remote manifest + listing locally."""
    inserted_manifests = updated_listings = 0
    for entry in fetch_all():
        try:
            mf = await register_manifest(
                db, entry.get("manifest", {}),
                code_url=entry.get("manifest", {}).get("code_url"),
            )
        except Exception as e:                                          # noqa: BLE001
            logger.warning("marketplace.register failed: %s", e)
            continue
        if not mf:
            continue
        inserted_manifests += 1
        listing = (await db.execute(
            select(MarketplaceListing).where(
                MarketplaceListing.manifest_id == mf.id,
            )
        )).scalar_one_or_none()
        if listing is None:
            listing = MarketplaceListing(manifest_id=mf.id)
            db.add(listing)
        listing.category = entry.get("category")
        listing.rating_avg = float(entry.get("rating_avg", 0) or 0)
        listing.ratings_count = int(entry.get("ratings_count", 0) or 0)
        listing.downloads = int(entry.get("downloads", 0) or 0)
        listing.screenshots = entry.get("screenshots") or []
        listing.featured = bool(entry.get("featured", False))
        listing.tags = entry.get("tags") or []
        listing.long_description = entry.get("long_description")
        listing.listing_status = entry.get("listing_status", "approved")
        updated_listings += 1
    await db.commit()
    return {
        "manifests": inserted_manifests,
        "listings": updated_listings,
    }


async def browse_marketplace(
    db: AsyncSession,
    *, q: str | None = None, category: str | None = None,
    featured_only: bool = False, limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    stmt = (
        select(MarketplaceListing, PluginManifest)
        .join(PluginManifest, PluginManifest.id == MarketplaceListing.manifest_id)
        .where(MarketplaceListing.listing_status == "approved")
    )
    if category:
        stmt = stmt.where(MarketplaceListing.category == category)
    if featured_only:
        stmt = stmt.where(MarketplaceListing.featured.is_(True))
    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import func, or_
        stmt = stmt.where(or_(
            func.lower(PluginManifest.name).like(like),
            func.lower(PluginManifest.slug).like(like),
            func.lower(PluginManifest.description).like(like),
        ))
    stmt = stmt.order_by(MarketplaceListing.featured.desc(),
                         MarketplaceListing.downloads.desc()).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "manifest_id": mf.id, "slug": mf.slug, "name": mf.name,
            "version": mf.version, "author": mf.author,
            "description": mf.description, "homepage": mf.homepage,
            "permissions": mf.permissions, "hooks": mf.hooks_subscribed,
            "category": listing.category,
            "rating_avg": float(listing.rating_avg or 0),
            "ratings_count": listing.ratings_count,
            "downloads": listing.downloads,
            "screenshots": list(listing.screenshots or []),
            "featured": listing.featured,
            "tags": list(listing.tags or []),
            "long_description": listing.long_description,
        }
        for listing, mf in rows
    ]
