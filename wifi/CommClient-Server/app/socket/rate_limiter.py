"""
Enhanced socket event rate limiter with:
  - Per-event sliding window limits
  - Global aggregate rate limit per user
  - Per-user connection count limits
  - Automatic cleanup on disconnect

Production hardening:
  - Prevents socket event flooding
  - Prevents connection exhaustion
  - Logs rate limit violations for audit
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Rate Limit Configuration ────────────────────────────

# (max_events, window_seconds)
EVENT_LIMITS: dict[str, tuple[int, float]] = {
    # Messaging
    "v2_chat_send_message": (10, 1.0),         # 10 msgs/sec
    "v2_chat_typing_start": (5, 1.0),
    "v2_chat_typing_stop": (5, 1.0),
    "v2_chat_mark_delivered": (5, 1.0),
    "v2_chat_mark_read": (5, 1.0),
    "v2_chat_edit_message": (5, 1.0),
    "v2_chat_delete_message": (3, 1.0),
    "v2_chat_reaction": (5, 1.0),
    # Call signaling — ICE can burst but capped
    "signal_offer": (10, 1.0),
    "signal_answer": (10, 1.0),
    "signal_ice_candidate": (30, 1.0),          # ICE bursts are common
    "call_signal": (30, 1.0),
    # Call lifecycle
    "call_initiate": (2, 5.0),                  # 2 calls per 5 sec
    "v2_call_initiate": (2, 5.0),
    "call_accept": (2, 1.0),
    "call_reject": (2, 1.0),
    "call_hangup": (2, 1.0),
    "call_join_group": (2, 5.0),
    "v2_call_join_group": (2, 5.0),
    # Sync
    "sync_request": (2, 10.0),                  # 2 syncs per 10 sec
    "sync_unread_counts": (2, 5.0),
    # Screen share
    "call_screen_share_start": (3, 5.0),
    "call_screen_share_stop": (3, 5.0),
    "presenter_request": (3, 5.0),
    # Default fallback — tightened from 30 to 15
    "__default__": (15, 1.0),
}

# Global aggregate: max total events per user across all event types
GLOBAL_RATE_LIMIT_MAX = 100
GLOBAL_RATE_LIMIT_WINDOW = 1.0  # seconds

# Max concurrent socket connections per user
MAX_CONNECTIONS_PER_USER = 5


class SocketRateLimiter:
    """
    Sliding window rate limiter with per-user per-event tracking,
    global aggregate enforcement, and connection count limits.
    """

    def __init__(self) -> None:
        # user_id → event_name → deque of timestamps
        self._windows: dict[str, dict[str, deque[float]]] = defaultdict(
            lambda: defaultdict(deque)
        )
        # user_id → deque of all event timestamps (for global rate)
        self._global_windows: dict[str, deque[float]] = defaultdict(deque)
        # user_id → connection count
        self._connection_counts: dict[str, int] = defaultdict(int)

    def check(self, user_id: str, event: str) -> bool:
        """
        Check if the event is allowed under rate limits.
        Returns True if allowed, False if rate-limited.
        """
        now = time.monotonic()

        # 1. Global aggregate check
        global_window = self._global_windows[user_id]
        cutoff = now - GLOBAL_RATE_LIMIT_WINDOW
        while global_window and global_window[0] < cutoff:
            global_window.popleft()
        if len(global_window) >= GLOBAL_RATE_LIMIT_MAX:
            logger.warning("rate_limit_global", user_id=user_id, event=event,
                           count=len(global_window))
            return False
        global_window.append(now)

        # 2. Per-event check
        max_events, window_sec = EVENT_LIMITS.get(event, EVENT_LIMITS["__default__"])
        window = self._windows[user_id][event]
        cutoff = now - window_sec
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= max_events:
            logger.warning("rate_limit_event", user_id=user_id, event=event,
                           count=len(window), limit=max_events)
            return False

        window.append(now)
        return True

    # ── Connection count management ──────────────────────

    def can_connect(self, user_id: str) -> bool:
        """Check if user can open another connection."""
        return self._connection_counts.get(user_id, 0) < MAX_CONNECTIONS_PER_USER

    def record_connect(self, user_id: str) -> None:
        """Record a new connection for user."""
        self._connection_counts[user_id] = self._connection_counts.get(user_id, 0) + 1

    def record_disconnect(self, user_id: str) -> None:
        """Record a disconnection for user."""
        count = self._connection_counts.get(user_id, 0)
        if count > 1:
            self._connection_counts[user_id] = count - 1
        else:
            self._connection_counts.pop(user_id, None)

    def get_connection_count(self, user_id: str) -> int:
        """Get current connection count for user."""
        return self._connection_counts.get(user_id, 0)

    # ── Cleanup ──────────────────────────────────────────

    def cleanup_user(self, user_id: str) -> None:
        """Remove all rate limit state for a user (on full disconnect)."""
        self._windows.pop(user_id, None)
        self._global_windows.pop(user_id, None)
        # Don't clear connection count here — handled by record_disconnect

    def sweep_expired(self) -> dict:
        """Periodic compaction: drop expired timestamps and remove
        users with no recent activity AND no live connections. Without
        this, a long-running server accumulates per-user dicts for
        every (user, event) pair the user ever fired — even if the
        user disconnected uncleanly (lost network, browser crash).
        Returns counts of what was reclaimed."""
        now = time.monotonic()
        users_dropped = 0
        events_dropped = 0
        for uid in list(self._windows.keys()):
            event_windows = self._windows[uid]
            for ev_name in list(event_windows.keys()):
                _, window_sec = EVENT_LIMITS.get(
                    ev_name, EVENT_LIMITS["__default__"],
                )
                w = event_windows[ev_name]
                cutoff = now - window_sec
                while w and w[0] < cutoff:
                    w.popleft()
                if not w:
                    del event_windows[ev_name]
                    events_dropped += 1
            if not event_windows:
                del self._windows[uid]
                # Also drop global window if user is fully idle and
                # has no live connections (otherwise we'd erase live
                # rate-limit state for an active session).
                if self._connection_counts.get(uid, 0) == 0:
                    self._global_windows.pop(uid, None)
                    users_dropped += 1
        # Trim global windows in-place too.
        for uid in list(self._global_windows.keys()):
            gw = self._global_windows[uid]
            cutoff = now - GLOBAL_RATE_LIMIT_WINDOW
            while gw and gw[0] < cutoff:
                gw.popleft()
            if not gw and self._connection_counts.get(uid, 0) == 0:
                del self._global_windows[uid]
                users_dropped += 1
        return {
            "users_dropped":  users_dropped,
            "events_dropped": events_dropped,
            "users_tracked":  len(self._windows),
        }


# Singleton
socket_rate_limiter = SocketRateLimiter()
