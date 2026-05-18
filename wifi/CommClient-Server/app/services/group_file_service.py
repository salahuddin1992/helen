"""
GroupFileService — orchestrates multicast file offers + BitTorrent-style
swarm bookkeeping on top of the ``group_file_offers`` and
``group_file_chunk_availability`` tables.

Responsibilities
----------------
  * Accept a finished ``FileRecord`` upload and fan it out to every
    member of a channel as per-recipient lifecycle rows.
  * Drive the per-recipient state machine
    (``pending → accepted → completed`` / ``declined``).
  * Track which chunks each peer currently holds via a packed bitmap so
    we can answer ``get_chunk_peers(offer_id, chunk_index)`` in
    near-constant time (bitmap bit test over at most N peers).
  * Sweep expired / abandoned offers on a background cadence.

Concurrency / ordering notes
----------------------------
  * Every mutation path commits. Callers that need to stack multiple
    updates should batch at the call site rather than nesting commits.
  * We do not serialise bitmap updates with an application-level lock —
    SQLite's connection-level writer lock is enough for the single-
    process server; if we ever migrate to Postgres with multiple workers
    we should either add ``SELECT ... FOR UPDATE`` or move to an
    append-only chunk-events table and rebuild the bitmap server-side.
  * Counter increments on ``GroupFileOffer`` are read-modify-write.
    Acceptable for the current deployment topology (one worker); revisit
    when sharding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.file import FileRecord
from app.models.group_file_offer import (
    AVAIL_ACTIVE_STATUSES,
    AVAIL_STATUS_ABANDONED,
    AVAIL_STATUS_ACCEPTED,
    AVAIL_STATUS_COMPLETED,
    AVAIL_STATUS_DECLINED,
    AVAIL_STATUS_PENDING,
    GroupFileChunkAvailability,
    GroupFileOffer,
    OFFER_ACTIVE_STATUSES,
    OFFER_STATUS_ACTIVE,
    OFFER_STATUS_CANCELLED,
    OFFER_STATUS_COMPLETED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
)
from app.services.channel_service import ChannelService

logger = get_logger(__name__)


# ── Tunables ───────────────────────────────────────────────────────

# Default offer lifetime when the caller doesn't pick one.
DEFAULT_OFFER_TTL = timedelta(hours=24)
# Upper bound — don't let clients schedule week-long offers.
MAX_OFFER_TTL = timedelta(days=7)
# A recipient that doesn't make progress for this long is swept to
# ``abandoned`` by ``cleanup_stale_recipients``.
STALE_RECIPIENT_GRACE = timedelta(hours=6)
# Minimum allowed chunk size to prevent pathological fan-out (1 byte ×
# N million chunks). Mirrors the resumable upload defaults.
MIN_CHUNK_SIZE = 64 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024
# Cap on total_chunks to prevent gigantic bitmaps. Audit fix H-4:
# lowered from 1M (125KB bitmap × N recipients = MB of DB rows) to
# 200K (25KB × N) which is still enough for ~12 GB at 64 KiB chunks.
# Operators on truly huge files can raise via HELEN_GROUP_FILE_MAX_CHUNKS.
import os as _os_chunks_max
try:
    _env_chunks_max = int(_os_chunks_max.environ.get(
        "HELEN_GROUP_FILE_MAX_CHUNKS", "200000",
    ))
except ValueError:
    _env_chunks_max = 200_000
MAX_TOTAL_CHUNKS = max(1024, _env_chunks_max)


class GroupFileService:

    # ── Offer creation ─────────────────────────────────────────────

    @staticmethod
    async def create_offer(
        db: AsyncSession,
        *,
        sender_id: str,
        channel_id: str,
        file_id: str,
        chunk_size: int,
        total_chunks: int,
        caption: str | None = None,
        swarm_enabled: bool = True,
        expires_in: timedelta | None = None,
        checksum: str | None = None,
        compute_chunk_hashes: bool = False,
    ) -> GroupFileOffer:
        """
        Register a new multicast offer. The ``FileRecord`` must already
        exist (uploaded via the normal resumable-upload flow). We create
        one ``GroupFileChunkAvailability`` row per channel member except
        the sender.
        """

        # ── Validation ──
        if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
            raise ValidationError(
                f"chunk_size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE}",
            )
        if total_chunks <= 0 or total_chunks > MAX_TOTAL_CHUNKS:
            raise ValidationError(
                f"total_chunks must be between 1 and {MAX_TOTAL_CHUNKS}",
            )

        if expires_in is None:
            ttl = DEFAULT_OFFER_TTL
        else:
            ttl = min(expires_in, MAX_OFFER_TTL)
        if ttl.total_seconds() <= 0:
            raise ValidationError("expires_in must be positive")

        # Channel membership (sender must be a member).
        if not await ChannelService.is_member(db, channel_id, sender_id):
            raise ForbiddenError("sender is not a member of the channel")

        # File must exist.
        file_row = await db.get(FileRecord, file_id)
        if file_row is None:
            raise NotFoundError("FileRecord", file_id)

        # Gather recipient set (all current members minus the sender).
        member_rows = await db.execute(
            select(ChannelMember.user_id).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id != sender_id,
            )
        )
        recipient_ids = [r for (r,) in member_rows.all()]

        # Audit fix H-2: compute per-chunk SHA-256 prefixes if the
        # sender opted in. The hashes let recipients reject corrupt /
        # tampered chunks before counting them as received. We use an
        # 8-byte truncated prefix to keep the offer payload bounded
        # (200K chunks × 8 bytes = 1.6 MB JSON; v1 sends None).
        chunk_hashes_json: str | None = None
        if compute_chunk_hashes and total_chunks <= MAX_TOTAL_CHUNKS:
            try:
                import base64 as _b64
                import hashlib as _h
                import json as _json
                hashes: list[str] = []
                # Read the file streaming to keep memory bounded.
                with open(file_row.storage_path, "rb") as fh:
                    for _i in range(total_chunks):
                        buf = fh.read(chunk_size)
                        if not buf:
                            break
                        h = _h.sha256(buf).digest()[:8]
                        hashes.append(_b64.b64encode(h).decode("ascii"))
                chunk_hashes_json = _json.dumps(hashes)
            except Exception as _hash_e:
                logger.warning(
                    "group_file_chunk_hashes_failed",
                    file_id=file_id, error=str(_hash_e),
                )

        now = datetime.now(timezone.utc)
        offer = GroupFileOffer(
            sender_id=sender_id,
            channel_id=channel_id,
            file_id=file_id,
            filename=file_row.original_name,
            file_size=file_row.size_bytes,
            mime_type=file_row.mime_type,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            checksum=checksum or file_row.checksum_sha256,
            caption=caption,
            status=OFFER_STATUS_OFFERED,
            swarm_enabled=bool(swarm_enabled),
            accepted_count=0,
            rejected_count=0,
            completed_count=0,
            expected_recipients=len(recipient_ids),
            expires_at=now + ttl,
            chunk_hashes_json=chunk_hashes_json,
        )
        db.add(offer)
        await db.flush()  # materialise offer.id before we key availability rows

        for uid in recipient_ids:
            db.add(GroupFileChunkAvailability(
                offer_id=offer.id,
                user_id=uid,
                status=AVAIL_STATUS_PENDING,
            ))

        await db.commit()
        await db.refresh(offer)

        logger.info(
            "group_file_offer_created",
            offer_id=offer.id,
            channel_id=channel_id,
            sender_id=sender_id,
            file_id=file_id,
            recipients=len(recipient_ids),
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            swarm=swarm_enabled,
        )
        return offer

    # ── Lookups ────────────────────────────────────────────────────

    @staticmethod
    async def get_offer(
        db: AsyncSession, offer_id: str, *, with_availabilities: bool = False
    ) -> GroupFileOffer:
        query = select(GroupFileOffer).where(GroupFileOffer.id == offer_id)
        if with_availabilities:
            query = query.options(selectinload(GroupFileOffer.availabilities))
        row = (await db.execute(query)).scalar_one_or_none()
        if row is None:
            raise NotFoundError("GroupFileOffer", offer_id)
        return row

    @staticmethod
    async def list_offers_for_channel(
        db: AsyncSession,
        channel_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[GroupFileOffer]:
        q = select(GroupFileOffer).where(GroupFileOffer.channel_id == channel_id)
        if status:
            q = q.where(GroupFileOffer.status == status)
        q = (
            q.order_by(GroupFileOffer.created_at.desc())
            .limit(min(max(limit, 1), 500))
            .offset(max(offset, 0))
        )
        return list((await db.execute(q)).scalars().all())

    @staticmethod
    async def list_offers_for_user(
        db: AsyncSession,
        user_id: str,
        *,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[GroupFileOffer]:
        """
        Offers the user is a *recipient* of. Used by the client to
        render the "incoming files" pane.
        """
        q = (
            select(GroupFileOffer)
            .join(
                GroupFileChunkAvailability,
                GroupFileChunkAvailability.offer_id == GroupFileOffer.id,
            )
            .where(GroupFileChunkAvailability.user_id == user_id)
        )
        if active_only:
            q = q.where(GroupFileOffer.status.in_(tuple(OFFER_ACTIVE_STATUSES)))
        q = q.order_by(GroupFileOffer.created_at.desc()).limit(min(max(limit, 1), 500))
        return list((await db.execute(q)).scalars().all())

    @staticmethod
    async def _get_availability(
        db: AsyncSession, offer_id: str, user_id: str
    ) -> GroupFileChunkAvailability:
        row = await db.get(GroupFileChunkAvailability, (offer_id, user_id))
        if row is None:
            raise NotFoundError(
                "GroupFileChunkAvailability", f"{offer_id}:{user_id}",
            )
        return row

    # ── Accept / reject ────────────────────────────────────────────

    @staticmethod
    async def accept_offer(
        db: AsyncSession, offer_id: str, user_id: str
    ) -> tuple[GroupFileOffer, GroupFileChunkAvailability]:
        """
        Mark a recipient as having accepted the offer. Returns the offer
        and the updated availability row. Idempotent on re-accept.
        """
        offer = await GroupFileService.get_offer(db, offer_id)
        if not offer.is_active():
            raise ValidationError(f"offer is not active (status={offer.status})")

        row = await GroupFileService._get_availability(db, offer_id, user_id)

        # Re-accept is a no-op from this point on but we still refresh
        # timestamps so the client sees an ack.
        if row.status == AVAIL_STATUS_ACCEPTED:
            return offer, row
        if row.status in (AVAIL_STATUS_COMPLETED, AVAIL_STATUS_DECLINED,
                          AVAIL_STATUS_ABANDONED):
            raise ValidationError(
                f"availability already terminal (status={row.status})",
            )

        row.mark_status(AVAIL_STATUS_ACCEPTED)
        offer.accepted_count = (offer.accepted_count or 0) + 1

        # First acceptance flips the offer from 'offered' → 'active'.
        if offer.status == OFFER_STATUS_OFFERED:
            offer.mark_status(OFFER_STATUS_ACTIVE)

        await db.commit()
        await db.refresh(offer)
        await db.refresh(row)

        logger.info(
            "group_file_offer_accepted",
            offer_id=offer.id, user_id=user_id,
            accepted=offer.accepted_count,
            expected=offer.expected_recipients,
        )
        return offer, row

    @staticmethod
    async def reject_offer(
        db: AsyncSession, offer_id: str, user_id: str
    ) -> tuple[GroupFileOffer, GroupFileChunkAvailability]:
        offer = await GroupFileService.get_offer(db, offer_id)
        row = await GroupFileService._get_availability(db, offer_id, user_id)

        if row.status == AVAIL_STATUS_DECLINED:
            return offer, row
        if row.status in (AVAIL_STATUS_COMPLETED, AVAIL_STATUS_ABANDONED):
            raise ValidationError(
                f"availability already terminal (status={row.status})",
            )

        row.mark_status(AVAIL_STATUS_DECLINED)
        offer.rejected_count = (offer.rejected_count or 0) + 1

        # If everyone has responded (and none are still pending/accepted)
        # flip to completed when all non-decliners are done.
        await GroupFileService._maybe_close_offer(db, offer)

        await db.commit()
        await db.refresh(offer)
        await db.refresh(row)

        logger.info(
            "group_file_offer_rejected",
            offer_id=offer.id, user_id=user_id,
            rejected=offer.rejected_count,
        )
        return offer, row

    # ── Chunk progress + swarm ────────────────────────────────────

    @staticmethod
    async def report_chunk_received(
        db: AsyncSession,
        offer_id: str,
        user_id: str,
        chunk_index: int,
        *,
        chunk_bytes: int | None = None,
    ) -> tuple[GroupFileChunkAvailability, bool, bool]:
        """
        Record that ``user_id`` now holds ``chunk_index`` for this offer.

        Returns ``(availability, flipped, became_complete)``:
          * ``flipped`` is True when the bit went 0 → 1 (so the caller
            can decide whether to fan out a ``group_peer_available``
            event).
          * ``became_complete`` is True when this report pushed the peer
            to a full bitmap for the first time.
        """
        offer = await GroupFileService.get_offer(db, offer_id)
        if not offer.is_active():
            raise ValidationError(f"offer is not active (status={offer.status})")
        if chunk_index < 0 or chunk_index >= offer.total_chunks:
            raise ValidationError(
                f"chunk_index {chunk_index} out of range [0, {offer.total_chunks})",
            )

        row = await GroupFileService._get_availability(db, offer_id, user_id)
        if row.status not in AVAIL_ACTIVE_STATUSES:
            raise ValidationError(
                f"availability not active (status={row.status})",
            )
        # Auto-promote: reporting implies acceptance.
        if row.status == AVAIL_STATUS_PENDING:
            row.mark_status(AVAIL_STATUS_ACCEPTED)
            offer.accepted_count = (offer.accepted_count or 0) + 1
            if offer.status == OFFER_STATUS_OFFERED:
                offer.mark_status(OFFER_STATUS_ACTIVE)

        flipped = row.set_chunk(chunk_index, offer.total_chunks)
        now = datetime.now(timezone.utc)
        row.last_progress_at = now
        if flipped:
            row.chunks_received = (row.chunks_received or 0) + 1
            if chunk_bytes and chunk_bytes > 0:
                row.bytes_received = (row.bytes_received or 0) + int(chunk_bytes)

        became_complete = False
        if row.is_complete(offer.total_chunks) and row.status != AVAIL_STATUS_COMPLETED:
            row.mark_status(AVAIL_STATUS_COMPLETED)
            row.completed_at = now
            offer.completed_count = (offer.completed_count or 0) + 1
            became_complete = True
            # Whole offer finished?
            await GroupFileService._maybe_close_offer(db, offer)

        await db.commit()
        await db.refresh(offer)
        await db.refresh(row)

        if flipped:
            logger.debug(
                "group_file_chunk_reported",
                offer_id=offer.id, user_id=user_id,
                chunk=chunk_index, chunks_total=row.chunks_received,
                became_complete=became_complete,
            )
        return row, flipped, became_complete

    @staticmethod
    async def get_chunk_peers(
        db: AsyncSession,
        offer_id: str,
        chunk_index: int,
        *,
        exclude_user_id: str | None = None,
        limit: int = 32,
    ) -> list[str]:
        """
        Return user_ids of peers currently holding ``chunk_index`` for
        this offer. Sender is always added as an implicit source (server
        storage), represented by the sender_id — callers can dedup.
        """
        offer = await GroupFileService.get_offer(db, offer_id)
        if chunk_index < 0 or chunk_index >= offer.total_chunks:
            raise ValidationError(
                f"chunk_index {chunk_index} out of range [0, {offer.total_chunks})",
            )

        # Swarm disabled → only the sender can serve.
        if not offer.swarm_enabled:
            return [offer.sender_id] if offer.sender_id != exclude_user_id else []

        q = select(GroupFileChunkAvailability).where(
            GroupFileChunkAvailability.offer_id == offer_id,
            GroupFileChunkAvailability.status.in_(
                (AVAIL_STATUS_ACCEPTED, AVAIL_STATUS_COMPLETED),
            ),
        )
        if exclude_user_id:
            q = q.where(GroupFileChunkAvailability.user_id != exclude_user_id)
        rows = list((await db.execute(q)).scalars().all())

        peers: list[str] = []
        for r in rows:
            if r.has_chunk(chunk_index):
                peers.append(r.user_id)
                if len(peers) >= limit:
                    break

        # Sender always available (server side storage).
        if offer.sender_id != exclude_user_id and offer.sender_id not in peers:
            peers.append(offer.sender_id)
        return peers

    # ── Cancel / sweep ─────────────────────────────────────────────

    @staticmethod
    async def cancel_offer(
        db: AsyncSession, offer_id: str, requester_id: str
    ) -> GroupFileOffer:
        offer = await GroupFileService.get_offer(db, offer_id, with_availabilities=True)
        # Authorization: ONLY the original sender or a channel
        # admin/moderator may cancel an active offer. Any other
        # member of the channel is rejected — closing the audit gap
        # where the socket-level handler relied on the REST layer for
        # auth, leaving the socket entry-point open. This is the
        # single choke point for both REST and Socket.IO callers.
        if offer.sender_id != requester_id:
            from sqlalchemy import select as _sel
            from app.models.channel import ChannelMember as _CM
            row = (await db.execute(
                _sel(_CM).where(
                    _CM.channel_id == offer.channel_id,
                    _CM.user_id == requester_id,
                )
            )).scalar_one_or_none()
            if row is None:
                raise ForbiddenError("not a member of the channel")
            if row.role not in ("admin", "moderator"):
                raise ForbiddenError(
                    "Only the sender or a channel admin can cancel this offer.",
                )
        if offer.is_terminal():
            return offer
        offer.mark_status(OFFER_STATUS_CANCELLED)
        now = datetime.now(timezone.utc)
        for row in offer.availabilities:
            if row.status in AVAIL_ACTIVE_STATUSES:
                row.mark_status(AVAIL_STATUS_ABANDONED)
                if not row.completed_at:
                    row.completed_at = now
        await db.commit()
        await db.refresh(offer)
        logger.info("group_file_offer_cancelled", offer_id=offer.id,
                    by=requester_id)
        return offer

    @staticmethod
    async def _maybe_close_offer(db: AsyncSession, offer: GroupFileOffer) -> None:
        """
        If every recipient has reached a terminal state AND at least one
        completed, flip the offer to ``completed``. No commit.
        """
        # Count still-active rows.
        still_active = await db.execute(
            select(func.count(GroupFileChunkAvailability.user_id)).where(
                GroupFileChunkAvailability.offer_id == offer.id,
                GroupFileChunkAvailability.status.in_(tuple(AVAIL_ACTIVE_STATUSES)),
            )
        )
        if (still_active.scalar_one() or 0) > 0:
            return
        if offer.is_terminal():
            return
        offer.mark_status(OFFER_STATUS_COMPLETED)

    @staticmethod
    async def sweep_expired(db: AsyncSession) -> int:
        """
        Flip offers whose ``expires_at`` has passed to ``expired`` and
        abandon any still-active recipients. Returns the number of
        offers swept.
        """
        now = datetime.now(timezone.utc)
        q = select(GroupFileOffer).where(
            GroupFileOffer.status.in_(tuple(OFFER_ACTIVE_STATUSES)),
            GroupFileOffer.expires_at.is_not(None),
            GroupFileOffer.expires_at <= now,
        ).options(selectinload(GroupFileOffer.availabilities))
        rows = list((await db.execute(q)).scalars().all())
        if not rows:
            return 0

        for offer in rows:
            offer.mark_status(OFFER_STATUS_EXPIRED)
            for a in offer.availabilities:
                if a.status in AVAIL_ACTIVE_STATUSES:
                    a.mark_status(AVAIL_STATUS_ABANDONED)
                    if not a.completed_at:
                        a.completed_at = now
        await db.commit()
        logger.info("group_file_offers_expired", count=len(rows))
        return len(rows)

    @staticmethod
    async def cleanup_stale_recipients(
        db: AsyncSession, *, grace: timedelta | None = None,
    ) -> int:
        """
        Mark accepted recipients who haven't made progress in ``grace``
        as ``abandoned``. Returns the number of rows swept.
        """
        cutoff = datetime.now(timezone.utc) - (grace or STALE_RECIPIENT_GRACE)
        q = select(GroupFileChunkAvailability).where(
            GroupFileChunkAvailability.status == AVAIL_STATUS_ACCEPTED,
            GroupFileChunkAvailability.last_progress_at.is_not(None),
            GroupFileChunkAvailability.last_progress_at < cutoff,
        )
        rows = list((await db.execute(q)).scalars().all())
        if not rows:
            return 0
        for r in rows:
            r.mark_status(AVAIL_STATUS_ABANDONED)
            if not r.completed_at:
                r.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("group_file_stale_recipients_swept", count=len(rows))
        return len(rows)

    # ── Dashboard / stats ──────────────────────────────────────────

    @staticmethod
    async def get_offer_stats(db: AsyncSession, offer_id: str) -> dict:
        offer = await GroupFileService.get_offer(db, offer_id, with_availabilities=True)
        by_status: dict[str, int] = {}
        for a in offer.availabilities:
            by_status[a.status] = by_status.get(a.status, 0) + 1
        return {
            "offer": offer.to_dict(include_counts=True),
            "recipients": by_status,
            "total_recipients": len(offer.availabilities),
        }


__all__ = ["GroupFileService"]
