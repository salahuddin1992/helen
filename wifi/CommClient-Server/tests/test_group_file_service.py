"""
Unit tests for :mod:`app.services.group_file_service`.

Covers:

  * Bitmap helpers (set / has / held / is_complete) over the packed
    ``chunk_bitmap`` column.
  * Offer creation — expected_recipients excludes the sender and each
    member gets exactly one availability row.
  * accept_offer — pending → accepted, first acceptance promotes the
    offer to 'active', accept is idempotent.
  * reject_offer — pending → declined, counters tick, terminal on repeat.
  * report_chunk_received — sets bits, auto-promotes from pending,
    flags ``became_complete`` on last chunk, increments counters.
  * get_chunk_peers — returns accepted/completed peers that have the
    chunk, includes sender implicitly, honours swarm_enabled=False.
  * cancel_offer — flips pending/accepted rows to abandoned.
  * sweep_expired — expires past expires_at offers and abandons their
    still-active recipients.
  * cleanup_stale_recipients — abandons accepted recipients that
    stopped reporting.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models.channel import Channel, ChannelMember
from app.models.file import FileRecord
from app.models.group_file_offer import (
    AVAIL_STATUS_ABANDONED,
    AVAIL_STATUS_ACCEPTED,
    AVAIL_STATUS_COMPLETED,
    AVAIL_STATUS_DECLINED,
    AVAIL_STATUS_PENDING,
    GroupFileChunkAvailability,
    GroupFileOffer,
    OFFER_STATUS_ACTIVE,
    OFFER_STATUS_CANCELLED,
    OFFER_STATUS_COMPLETED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
)
from app.models.user import User
from app.services.group_file_service import GroupFileService


pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def module_engine():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine


async def _rand_id() -> str:
    return uuid.uuid4().hex


async def _make_user(db, username: str | None = None) -> User:
    username = username or f"u_{uuid.uuid4().hex[:10]}"
    user = User(
        username=username,
        display_name=username,
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_channel(db, member_ids: list[str], creator_id: str) -> Channel:
    ch = Channel(
        type="group",
        name=f"ch_{uuid.uuid4().hex[:8]}",
        is_active=True,
        created_by=creator_id,
    )
    db.add(ch)
    await db.commit()
    await db.refresh(ch)
    for uid in member_ids:
        db.add(ChannelMember(
            channel_id=ch.id,
            user_id=uid,
            role=("admin" if uid == creator_id else "member"),
        ))
    await db.commit()
    await db.refresh(ch)
    return ch


async def _make_file(db, uploader_id: str, *, channel_id: str | None = None,
                     size: int = 4 * 1024 * 1024) -> FileRecord:
    f = FileRecord(
        uploader_id=uploader_id,
        channel_id=channel_id,
        original_name=f"f_{uuid.uuid4().hex[:6]}.bin",
        stored_name=uuid.uuid4().hex,
        mime_type="application/octet-stream",
        size_bytes=size,
        storage_path=f"/tmp/{uuid.uuid4().hex}",
        checksum_sha256="0" * 64,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return f


@pytest.fixture
async def scenario(module_engine):
    """
    Build a small scenario: sender + 3 recipients in one channel, plus a
    FileRecord. Cleanup afterwards: delete our rows to keep tests isolated.
    """
    async with async_session_factory() as db:
        sender = await _make_user(db)
        alice = await _make_user(db)
        bob = await _make_user(db)
        carol = await _make_user(db)
        ch = await _make_channel(
            db, [sender.id, alice.id, bob.id, carol.id], creator_id=sender.id,
        )
        f = await _make_file(db, sender.id, channel_id=ch.id)

    data = {
        "sender_id": sender.id,
        "alice_id": alice.id,
        "bob_id": bob.id,
        "carol_id": carol.id,
        "channel_id": ch.id,
        "file_id": f.id,
    }
    yield data

    # Teardown — cascade on the offer table removes availability rows.
    async with async_session_factory() as db:
        await db.execute(
            delete(GroupFileOffer).where(GroupFileOffer.channel_id == ch.id)
        )
        await db.commit()


# ─────────────────────────────────────────────────────────────────
# Bitmap helpers
# ─────────────────────────────────────────────────────────────────


async def test_bitmap_set_and_has():
    a = GroupFileChunkAvailability(offer_id="o", user_id="u")
    # total_chunks=20 → 3 bytes (0..7, 8..15, 16..19)
    assert a.set_chunk(0, 20) is True
    assert a.has_chunk(0) is True
    assert a.has_chunk(1) is False
    # Second call on same bit returns False (no flip).
    assert a.set_chunk(0, 20) is False
    # Set a bit in the last byte.
    assert a.set_chunk(17, 20) is True
    assert a.has_chunk(17) is True
    # held_chunk_indexes reports exactly the bits we set.
    assert a.held_chunk_indexes(20) == [0, 17]


async def test_bitmap_is_complete():
    a = GroupFileChunkAvailability(offer_id="o", user_id="u")
    for i in range(5):
        a.set_chunk(i, 5)
    assert a.is_complete(5) is True
    # Partial.
    b = GroupFileChunkAvailability(offer_id="o", user_id="u")
    for i in range(4):
        b.set_chunk(i, 5)
    assert b.is_complete(5) is False


async def test_bitmap_out_of_range_rejected():
    a = GroupFileChunkAvailability(offer_id="o", user_id="u")
    with pytest.raises(ValueError):
        a.set_chunk(10, 10)
    with pytest.raises(ValueError):
        a.set_chunk(-1, 10)


async def test_bitmap_empty_helpers():
    a = GroupFileChunkAvailability(offer_id="o", user_id="u")
    assert a.has_chunk(0) is False
    assert a.held_chunk_indexes(16) == []
    assert a.is_complete(0) is True  # trivial
    assert a.is_complete(1) is False


# ─────────────────────────────────────────────────────────────────
# create_offer
# ─────────────────────────────────────────────────────────────────


async def test_create_offer_populates_availability(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=4,
            caption="hi",
        )
        assert offer.status == OFFER_STATUS_OFFERED
        assert offer.expected_recipients == 3  # 4 members - sender

        avails = (await db.execute(
            GroupFileChunkAvailability.__table__.select().where(
                GroupFileChunkAvailability.offer_id == offer.id,
            )
        )).all()
        assert len(avails) == 3
        # No row for the sender.
        assert scenario["sender_id"] not in [r.user_id for r in avails]


async def test_create_offer_rejects_non_member(scenario):
    async with async_session_factory() as db:
        stranger = await _make_user(db)
    async with async_session_factory() as db:
        with pytest.raises(ForbiddenError):
            await GroupFileService.create_offer(
                db,
                sender_id=stranger.id,
                channel_id=scenario["channel_id"],
                file_id=scenario["file_id"],
                chunk_size=1024 * 1024,
                total_chunks=4,
            )


async def test_create_offer_validates_chunk_size(scenario):
    async with async_session_factory() as db:
        with pytest.raises(ValidationError):
            await GroupFileService.create_offer(
                db,
                sender_id=scenario["sender_id"],
                channel_id=scenario["channel_id"],
                file_id=scenario["file_id"],
                chunk_size=1,  # too small
                total_chunks=4,
            )
        with pytest.raises(ValidationError):
            await GroupFileService.create_offer(
                db,
                sender_id=scenario["sender_id"],
                channel_id=scenario["channel_id"],
                file_id=scenario["file_id"],
                chunk_size=1024 * 1024,
                total_chunks=0,  # must be > 0
            )


# ─────────────────────────────────────────────────────────────────
# accept_offer / reject_offer
# ─────────────────────────────────────────────────────────────────


async def test_accept_promotes_offer_and_is_idempotent(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=4,
        )
    async with async_session_factory() as db:
        offer, row = await GroupFileService.accept_offer(
            db, offer.id, scenario["alice_id"],
        )
        assert row.status == AVAIL_STATUS_ACCEPTED
        assert offer.status == OFFER_STATUS_ACTIVE
        assert offer.accepted_count == 1
    # Second accept is a no-op (counter doesn't double-tick).
    async with async_session_factory() as db:
        offer2, _ = await GroupFileService.accept_offer(
            db, offer.id, scenario["alice_id"],
        )
        assert offer2.accepted_count == 1


async def test_reject_blocks_subsequent_accept(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=4,
        )
    async with async_session_factory() as db:
        offer2, row = await GroupFileService.reject_offer(
            db, offer.id, scenario["alice_id"],
        )
        assert row.status == AVAIL_STATUS_DECLINED
        assert offer2.rejected_count == 1
    async with async_session_factory() as db:
        with pytest.raises(ValidationError):
            await GroupFileService.accept_offer(
                db, offer.id, scenario["alice_id"],
            )


# ─────────────────────────────────────────────────────────────────
# report_chunk_received
# ─────────────────────────────────────────────────────────────────


async def test_report_chunk_auto_promotes_and_flips(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=3,
        )
    async with async_session_factory() as db:
        row, flipped, complete = await GroupFileService.report_chunk_received(
            db, offer.id, scenario["alice_id"], 0, chunk_bytes=1024,
        )
        assert flipped is True
        assert complete is False
        assert row.status == AVAIL_STATUS_ACCEPTED  # auto-promoted
        assert row.chunks_received == 1
        assert row.bytes_received == 1024

    # Re-report same chunk: not flipped, counters unchanged.
    async with async_session_factory() as db:
        row2, flipped2, _ = await GroupFileService.report_chunk_received(
            db, offer.id, scenario["alice_id"], 0, chunk_bytes=1024,
        )
        assert flipped2 is False
        assert row2.chunks_received == 1


async def test_report_last_chunk_completes(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=2,
        )
    async with async_session_factory() as db:
        await GroupFileService.report_chunk_received(
            db, offer.id, scenario["alice_id"], 0,
        )
    async with async_session_factory() as db:
        row, flipped, became_complete = (
            await GroupFileService.report_chunk_received(
                db, offer.id, scenario["alice_id"], 1,
            )
        )
        assert became_complete is True
        assert row.status == AVAIL_STATUS_COMPLETED
        assert row.completed_at is not None


# ─────────────────────────────────────────────────────────────────
# get_chunk_peers
# ─────────────────────────────────────────────────────────────────


async def test_get_chunk_peers_includes_sender_and_holders(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=4,
        )
    # Alice and Bob get chunk 0.
    for uid in (scenario["alice_id"], scenario["bob_id"]):
        async with async_session_factory() as db:
            await GroupFileService.report_chunk_received(
                db, offer.id, uid, 0,
            )
    async with async_session_factory() as db:
        # From Carol's perspective: she should see Alice + Bob + Sender.
        peers = await GroupFileService.get_chunk_peers(
            db, offer.id, 0, exclude_user_id=scenario["carol_id"],
        )
        assert scenario["alice_id"] in peers
        assert scenario["bob_id"] in peers
        assert scenario["sender_id"] in peers
        # Nobody has chunk 3 yet — only sender.
        peers3 = await GroupFileService.get_chunk_peers(
            db, offer.id, 3, exclude_user_id=scenario["carol_id"],
        )
        assert peers3 == [scenario["sender_id"]]


async def test_get_chunk_peers_respects_swarm_disabled(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=3,
            swarm_enabled=False,
        )
    async with async_session_factory() as db:
        await GroupFileService.report_chunk_received(
            db, offer.id, scenario["alice_id"], 0,
        )
    async with async_session_factory() as db:
        peers = await GroupFileService.get_chunk_peers(
            db, offer.id, 0, exclude_user_id=scenario["bob_id"],
        )
        # Swarm off — only the sender is a legal source.
        assert peers == [scenario["sender_id"]]


# ─────────────────────────────────────────────────────────────────
# cancel_offer / sweep_expired
# ─────────────────────────────────────────────────────────────────


async def test_cancel_flips_active_recipients_to_abandoned(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=3,
        )
    async with async_session_factory() as db:
        await GroupFileService.accept_offer(
            db, offer.id, scenario["alice_id"],
        )
    async with async_session_factory() as db:
        cancelled = await GroupFileService.cancel_offer(
            db, offer.id, scenario["sender_id"],
        )
        assert cancelled.status == OFFER_STATUS_CANCELLED
    async with async_session_factory() as db:
        alice_row = await db.get(
            GroupFileChunkAvailability,
            (offer.id, scenario["alice_id"]),
        )
        bob_row = await db.get(
            GroupFileChunkAvailability,
            (offer.id, scenario["bob_id"]),
        )
        assert alice_row.status == AVAIL_STATUS_ABANDONED
        assert bob_row.status == AVAIL_STATUS_ABANDONED


async def test_sweep_expired_flips_past_offers(scenario):
    async with async_session_factory() as db:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=scenario["sender_id"],
            channel_id=scenario["channel_id"],
            file_id=scenario["file_id"],
            chunk_size=1024 * 1024,
            total_chunks=3,
        )
        # Force expiration into the past.
        offer.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    async with async_session_factory() as db:
        swept = await GroupFileService.sweep_expired(db)
        assert swept >= 1
    async with async_session_factory() as db:
        refreshed = await GroupFileService.get_offer(db, offer.id)
        assert refreshed.status == OFFER_STATUS_EXPIRED
