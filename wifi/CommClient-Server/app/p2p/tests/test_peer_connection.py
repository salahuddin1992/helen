"""Tests for app.p2p.peer_connection."""

from __future__ import annotations

import pytest

from app.p2p.p2p_exceptions import PeerConnectionError
from app.p2p.peer_connection import request
from app.p2p.peer_model import Peer, PeerRole


@pytest.mark.asyncio
async def test_request_to_unreachable_raises():
    """A peer pointed at a black-hole IP should raise."""
    peer = Peer(peer_id="unreach", role=PeerRole.NORMAL,
                host="240.0.0.1", port=12345)
    with pytest.raises(PeerConnectionError):
        await request(peer, method="GET", path="/", timeout=0.5)
