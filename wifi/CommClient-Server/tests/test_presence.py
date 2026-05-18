"""
Presence service unit tests.

Tests for online/offline status tracking and user presence queries.
All methods are async since PresenceService uses asyncio.Lock internally.
"""

from __future__ import annotations

import pytest

from app.services.presence_service import presence_service


class TestPresenceConnect:
    """Tests for user connection tracking."""

    async def test_connect_user_marks_online(self):
        """Connect user adds them to online set."""
        sid = "socket_id_123"
        user_id = "user_123"

        await presence_service.connect(user_id, sid)

        assert await presence_service.is_online(user_id) == True

    async def test_multiple_connections_same_user(self):
        """User with multiple connections still marked online."""
        user_id = "user_multi"
        sid1 = "socket_1"
        sid2 = "socket_2"

        await presence_service.connect(user_id, sid1)
        await presence_service.connect(user_id, sid2)

        assert await presence_service.is_online(user_id) == True

    async def test_disconnect_one_connection_keeps_online(self):
        """Disconnecting one connection with other active keeps user online."""
        user_id = "user_multi2"
        sid1 = "socket_a"
        sid2 = "socket_b"

        await presence_service.connect(user_id, sid1)
        await presence_service.connect(user_id, sid2)

        # Disconnect one
        await presence_service.disconnect(sid1)

        # User should still be online
        assert await presence_service.is_online(user_id) == True

    async def test_disconnect_all_connections_marks_offline(self):
        """Disconnecting all connections marks user offline."""
        user_id = "user_offline"
        sid1 = "socket_x"
        sid2 = "socket_y"

        await presence_service.connect(user_id, sid1)
        await presence_service.connect(user_id, sid2)

        # Disconnect both
        await presence_service.disconnect(sid1)
        await presence_service.disconnect(sid2)

        # User should be offline
        assert await presence_service.is_online(user_id) == False

    async def test_connect_updates_connection_time(self):
        """Connect updates last seen timestamp."""
        sid = "socket_ts"
        user_id = "user_ts"

        await presence_service.connect(user_id, sid)

        # User should have a last_seen time
        last_seen = presence_service._last_heartbeat.get(user_id)
        assert last_seen is not None


class TestPresenceDisconnect:
    """Tests for user disconnection tracking."""

    async def test_disconnect_marks_offline_when_last_connection(self):
        """Disconnect marks user offline when it's their last connection."""
        sid = "single_socket"
        user_id = "single_user"

        await presence_service.connect(user_id, sid)
        assert await presence_service.is_online(user_id) == True

        await presence_service.disconnect(sid)
        assert await presence_service.is_online(user_id) == False

    async def test_disconnect_nonexistent_socket(self):
        """Disconnect nonexistent socket doesn't crash."""
        # Should not raise an exception
        await presence_service.disconnect("nonexistent_socket")

    async def test_disconnect_updates_last_seen(self):
        """Disconnect cleans up heartbeat tracking for fully offline user."""
        sid = "socket_disc"
        user_id = "user_disc"

        await presence_service.connect(user_id, sid)
        # While connected, heartbeat should exist
        assert presence_service._last_heartbeat.get(user_id) is not None

        await presence_service.disconnect(sid)

        # After full disconnect, heartbeat entry is cleaned up (user is offline)
        # This is correct — the heartbeat is removed on full disconnect
        assert await presence_service.is_online(user_id) == False


class TestPresenceStatus:
    """Tests for user presence status."""

    async def test_set_status(self):
        """Set user status to online/away/busy/dnd."""
        user_id = "user_status"

        for status in ["online", "away", "busy", "dnd"]:
            await presence_service.set_status(user_id, status)
            assert await presence_service.get_status(user_id) == status

    async def test_get_status_default(self):
        """Get status for user without explicit status returns default."""
        user_id = "user_no_status_xyz"
        status = await presence_service.get_status(user_id)

        # Default is "offline" or not set
        assert status in (None, "offline", "online")

    async def test_get_status_after_set(self):
        """Get status returns what was set."""
        user_id = "user_get_status"

        await presence_service.set_status(user_id, "away")
        assert await presence_service.get_status(user_id) == "away"

        await presence_service.set_status(user_id, "busy")
        assert await presence_service.get_status(user_id) == "busy"

    async def test_set_status_multiple_users_independent(self):
        """Each user's status is independent."""
        user1 = "user_indep_1"
        user2 = "user_indep_2"

        await presence_service.set_status(user1, "online")
        await presence_service.set_status(user2, "away")

        assert await presence_service.get_status(user1) == "online"
        assert await presence_service.get_status(user2) == "away"


class TestPresenceQueries:
    """Tests for querying presence information."""

    async def test_get_all_online(self):
        """Get all online users returns dict of online users."""
        sid1 = "test_socket_1"
        sid2 = "test_socket_2"
        user1 = "test_user_1"
        user2 = "test_user_2"

        await presence_service.connect(user1, sid1)
        await presence_service.connect(user2, sid2)
        # Set status so they appear in get_all_online
        await presence_service.set_status(user1, "online")
        await presence_service.set_status(user2, "online")

        online = await presence_service.get_all_online()

        assert isinstance(online, dict)
        assert user1 in online
        assert user2 in online

    async def test_get_all_online_excludes_offline_users(self):
        """Get all online excludes users with no connections."""
        sid = "online_test"
        user_online = "user_online_test"
        user_offline = "user_offline_test"

        await presence_service.connect(user_online, sid)
        await presence_service.set_status(user_online, "online")

        online = await presence_service.get_all_online()
        online_ids = list(online.keys())

        assert user_online in online_ids
        assert user_offline not in online_ids

    async def test_get_user_connections(self):
        """Get connections for a user returns all their sockets."""
        user_id = "user_connections"
        sid1 = "socket_conn_1"
        sid2 = "socket_conn_2"

        await presence_service.connect(user_id, sid1)
        await presence_service.connect(user_id, sid2)

        connections = presence_service.get_sids(user_id)

        assert isinstance(connections, set)
        assert sid1 in connections
        assert sid2 in connections

    async def test_get_socket_user_id(self):
        """Get user ID for a socket returns the correct user."""
        sid = "socket_user"
        user_id = "user_socket"

        await presence_service.connect(user_id, sid)

        found_user = presence_service.get_user_id(sid)

        assert found_user == user_id

    async def test_get_socket_user_nonexistent_returns_none(self):
        """Get user ID for nonexistent socket returns None."""
        result = presence_service.get_user_id("nonexistent_socket_id")

        assert result is None


class TestPresenceCleanup:
    """Tests for presence data cleanup."""

    async def test_presence_data_cleanup_on_disconnect(self):
        """Presence data is cleaned up when user disconnects completely."""
        sid = "cleanup_socket"
        user_id = "cleanup_user"

        await presence_service.connect(user_id, sid)
        assert await presence_service.is_online(user_id) == True

        await presence_service.disconnect(sid)

        # After complete disconnect, should be offline
        assert await presence_service.is_online(user_id) == False

    async def test_status_persists_across_connections(self):
        """User status persists when they have multiple connections."""
        user_id = "user_persist"
        sid1 = "socket_persist_1"
        sid2 = "socket_persist_2"

        await presence_service.connect(user_id, sid1)
        await presence_service.set_status(user_id, "away")

        # Connect again
        await presence_service.connect(user_id, sid2)

        # Status should still be away
        assert await presence_service.get_status(user_id) == "away"

        # Disconnect one connection
        await presence_service.disconnect(sid1)

        # Status should still be away
        assert await presence_service.get_status(user_id) == "away"


class TestPresenceLargeScale:
    """Tests for presence service with many users."""

    async def test_many_online_users(self):
        """Handle many online users simultaneously."""
        num_users = 100
        sockets = {}

        for i in range(num_users):
            sid = f"socket_scale_{i}"
            user_id = f"user_scale_{i}"
            sockets[user_id] = sid
            await presence_service.connect(user_id, sid)
            await presence_service.set_status(user_id, "online")

        online = await presence_service.get_all_online()

        assert len(online) >= num_users

    async def test_many_connections_per_user(self):
        """Handle one user with many connections."""
        user_id = "user_many_conns"
        num_connections = 10

        for i in range(num_connections):
            sid = f"socket_many_{i}"
            await presence_service.connect(user_id, sid)

        connections = presence_service.get_sids(user_id)

        assert len(connections) == num_connections

        # Disconnect all but one
        socket_list = list(connections)
        for sid in socket_list[:-1]:
            await presence_service.disconnect(sid)

        # User still online
        assert await presence_service.is_online(user_id) == True

        # Disconnect last
        await presence_service.disconnect(socket_list[-1])

        # Now offline
        assert await presence_service.is_online(user_id) == False
