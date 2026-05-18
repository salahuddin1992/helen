"""DHT facade — Kademlia user-location lookup.

Helen's existing DHT lives in services.dht_kademlia + services.dht_lookup.
This file gives the p2p layer one read/write surface.
"""

from __future__ import annotations

from typing import Any, Optional


async def lookup(user_id: str) -> Optional[dict]:
    """Best-effort lookup of where a user is hosted."""
    try:
        from app.services.dht_lookup import lookup_user_location
        return await lookup_user_location(user_id)
    except Exception:
        return None


def store_local(user_id: str, server_id: str, ttl_sec: float = 120) -> bool:
    """Insert a (user_id → server_id) record into our local DHT shard."""
    try:
        from app.services.dht_kademlia import user_location_store
        user_location_store.put(user_id, server_id, ttl_sec=ttl_sec)
        return True
    except Exception:
        return False


def dht_snapshot() -> dict:
    try:
        from app.services.dht_kademlia import user_location_store
        return {"local_records": user_location_store.size()}
    except Exception:
        return {"local_records": 0}
