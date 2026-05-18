"""
Federation v2 — cross-server channel replication.

* ``share_channel(channel_id, with_server)`` — advertises a local
  channel to a remote server.
* ``unshare_channel(channel_id, with_server)`` — removes the share.
* ``sync_membership(channel_id)`` — full member-list resend.
* ``propagate_power_level(channel_id, user, level)`` — replicate role
  changes.
* ``broadcast_receipt(channel_id, user, message_id)`` — federate read
  receipts.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.federation_v2 import FederatedChannel, FederatedServer
from app.services.federation_v2.addressing import my_server_id
from app.services.federation_v2.signing import (
    event_hash, get_local_signing_key, sign_event,
)
from app.services.federation_v2.transport import get_transport

logger = get_logger(__name__)


async def share_channel(
    channel_id: str,
    with_server: str,
    *,
    policy: str = "public",
) -> FederatedChannel:
    """Make a local channel visible to a remote server."""
    sid = my_server_id()
    fed_addr = f"#{channel_id}@{sid}"
    async with async_session_factory() as db:
        row = (await db.execute(
            select(FederatedChannel).where(
                FederatedChannel.federation_address == fed_addr
            )
        )).scalar_one_or_none()
        if row is None:
            row = FederatedChannel(
                channel_id=channel_id,
                federation_address=fed_addr,
                origin_server=sid,
                shared_with=[with_server],
                policy=policy,
                state_version=1,
            )
            db.add(row)
        else:
            shared = list(row.shared_with or [])
            if with_server not in shared:
                shared.append(with_server)
            row.shared_with = shared
            row.state_version = (row.state_version or 0) + 1
        await db.commit()
        await db.refresh(row)
    # Send a membership "share" event so the remote can mirror metadata.
    await _emit_state_event(
        kind="state",
        channel_address=fed_addr,
        state_key="m.channel.shared",
        content={"shared_with": with_server, "policy": policy},
        target_servers=[with_server],
    )
    return row


async def unshare_channel(channel_id: str, with_server: str) -> None:
    sid = my_server_id()
    fed_addr = f"#{channel_id}@{sid}"
    async with async_session_factory() as db:
        row = (await db.execute(
            select(FederatedChannel).where(
                FederatedChannel.federation_address == fed_addr
            )
        )).scalar_one_or_none()
        if row is None:
            return
        shared = [s for s in (row.shared_with or []) if s != with_server]
        row.shared_with = shared
        row.state_version = (row.state_version or 0) + 1
        await db.commit()
    await _emit_state_event(
        kind="state",
        channel_address=fed_addr,
        state_key="m.channel.unshared",
        content={"removed": with_server},
        target_servers=[with_server],
    )


async def sync_membership(
    channel_id: str,
    member_addresses: list[str],
) -> None:
    """Push the authoritative member list to every shared server."""
    sid = my_server_id()
    fed_addr = f"#{channel_id}@{sid}"
    targets = await _shared_targets(fed_addr)
    if not targets:
        return
    await _emit_state_event(
        kind="state",
        channel_address=fed_addr,
        state_key="m.channel.members",
        content={"members": member_addresses, "synced_at": int(time.time())},
        target_servers=targets,
    )


async def propagate_power_level(
    channel_id: str, user_address: str, level: int,
) -> None:
    sid = my_server_id()
    fed_addr = f"#{channel_id}@{sid}"
    targets = await _shared_targets(fed_addr)
    if not targets:
        return
    await _emit_state_event(
        kind="state",
        channel_address=fed_addr,
        state_key=f"m.power_level:{user_address}",
        content={"user": user_address, "level": int(level)},
        target_servers=targets,
    )


async def broadcast_receipt(
    channel_id: str, user_address: str, message_id: str,
) -> None:
    sid = my_server_id()
    fed_addr = f"#{channel_id}@{sid}"
    targets = await _shared_targets(fed_addr)
    if not targets:
        return
    await _emit_state_event(
        kind="state",
        channel_address=fed_addr,
        state_key=f"m.read_receipt:{user_address}",
        content={"user": user_address, "message_id": message_id, "ts": int(time.time())},
        target_servers=targets,
    )


# ── internals ───────────────────────────────────────────────


async def _shared_targets(fed_addr: str) -> list[str]:
    async with async_session_factory() as db:
        row = (await db.execute(
            select(FederatedChannel).where(
                FederatedChannel.federation_address == fed_addr
            )
        )).scalar_one_or_none()
    if row is None:
        return []
    return list(row.shared_with or [])


async def _emit_state_event(
    *,
    kind: str,
    channel_address: str,
    state_key: str,
    content: dict[str, Any],
    target_servers: list[str],
) -> None:
    """Build, sign, and dispatch a state event."""
    sid = my_server_id()
    sk = get_local_signing_key()
    event: dict[str, Any] = {
        "type":      kind,
        "origin":    sid,
        "channel":   channel_address,
        "sender":    sid,
        "state_key": state_key,
        "ts":        int(time.time()),
        "depth":     0,
        "prev":      [],
        "content":   content,
    }
    event["event_id"] = event_hash(event)
    sign_event(event, sid, sk)
    transport = get_transport()
    for target in target_servers:
        try:
            await transport.push_event(target, event)
        except Exception as exc:
            logger.warning(
                "fedv2_replication_dispatch_failed target=%s err=%s",
                target, exc,
            )
