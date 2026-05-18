"""
Phase 7 / Module AH — Internal-only Plugin Ratings store.

Persists per-user, per-plugin ratings (1..5) with optional review text.
Aggregations (average + count) are computed on demand; no rolling
counters to keep writes simple. Heavy reads should call
:func:`aggregate` and cache for a minute or two upstream.

All endpoints in :mod:`admin_plugins` enforce ``require_role("admin")``;
this module assumes the caller already has authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.plugin import PluginManifest
from app.models.plugin_rating import PluginRating

logger = get_logger(__name__)


@dataclass
class RatingDTO:
    id: str
    manifest_slug: str
    manifest_id: Optional[str]
    user_id: str
    rating: int
    title: Optional[str]
    review: Optional[str]
    posted_at: Optional[str]

    @classmethod
    def from_orm(cls, row: PluginRating) -> "RatingDTO":
        return cls(
            id=row.id, manifest_slug=row.manifest_slug,
            manifest_id=row.manifest_id, user_id=row.user_id,
            rating=int(row.rating), title=row.title, review=row.review,
            posted_at=row.posted_at.isoformat() if row.posted_at else None,
        )


@dataclass
class RatingAggregate:
    manifest_slug: str
    average: float
    count: int
    histogram: dict[int, int]


# ───────────────────────────────────────────────────────────────────────


class RatingsStore:
    """Thin wrapper around the ``plugin_ratings`` table."""

    # ----- CRUD --------------------------------------------------------

    async def upsert(
        self,
        db: AsyncSession,
        *,
        slug: str,
        user_id: str,
        rating: int,
        title: Optional[str] = None,
        review: Optional[str] = None,
    ) -> RatingDTO:
        if not 1 <= int(rating) <= 5:
            raise ValueError("rating must be in 1..5")
        # Find manifest_id (best-effort)
        manifest_id = (await db.execute(
            select(PluginManifest.id)
            .where(PluginManifest.slug == slug)
            .order_by(PluginManifest.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

        existing = (await db.execute(
            select(PluginRating).where(
                PluginRating.manifest_slug == slug,
                PluginRating.user_id == user_id,
            )
        )).scalar_one_or_none()
        if existing:
            existing.rating = int(rating)
            existing.title = title
            existing.review = review
            existing.manifest_id = manifest_id or existing.manifest_id
            row = existing
        else:
            row = PluginRating(
                manifest_slug=slug, manifest_id=manifest_id,
                user_id=user_id, rating=int(rating),
                title=title, review=review,
            )
            db.add(row)
        await db.commit()
        await db.refresh(row)
        return RatingDTO.from_orm(row)

    async def list_for_plugin(
        self,
        db: AsyncSession,
        slug: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RatingDTO]:
        rows = (await db.execute(
            select(PluginRating)
            .where(PluginRating.manifest_slug == slug)
            .order_by(PluginRating.posted_at.desc())
            .offset(offset).limit(limit)
        )).scalars().all()
        return [RatingDTO.from_orm(r) for r in rows]

    async def aggregate(
        self,
        db: AsyncSession,
        slug: str,
    ) -> RatingAggregate:
        rows = (await db.execute(
            select(PluginRating.rating, func.count(PluginRating.id))
            .where(PluginRating.manifest_slug == slug)
            .group_by(PluginRating.rating)
        )).all()
        histogram = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        total = 0
        weighted = 0
        for rating, count in rows:
            r = int(rating)
            c = int(count)
            histogram[r] = c
            total += c
            weighted += r * c
        avg = (weighted / total) if total else 0.0
        return RatingAggregate(
            manifest_slug=slug, average=round(avg, 2),
            count=total, histogram=histogram,
        )

    async def delete(
        self,
        db: AsyncSession,
        *,
        slug: str,
        user_id: str,
    ) -> bool:
        result = await db.execute(
            delete(PluginRating).where(
                PluginRating.manifest_slug == slug,
                PluginRating.user_id == user_id,
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    async def get_for_user(
        self,
        db: AsyncSession,
        *,
        slug: str,
        user_id: str,
    ) -> Optional[RatingDTO]:
        row = (await db.execute(
            select(PluginRating).where(
                PluginRating.manifest_slug == slug,
                PluginRating.user_id == user_id,
            )
        )).scalar_one_or_none()
        return RatingDTO.from_orm(row) if row else None


_store = RatingsStore()


def get_ratings_store() -> RatingsStore:
    return _store


def rating_dto_to_dict(d: RatingDTO) -> dict[str, Any]:
    return asdict(d)


__all__ = [
    "RatingsStore", "RatingDTO", "RatingAggregate",
    "get_ratings_store", "rating_dto_to_dict",
]
