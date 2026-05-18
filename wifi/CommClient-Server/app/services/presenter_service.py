"""
PresenterService — in-memory presenter state management.

Tracks which user holds the presenter lock for each active call.
Only one presenter allowed at a time per call. Supports:
  - Presenter lock/unlock
  - Queued requests (FIFO)
  - Auto-promotion from queue
  - Force-stop (admin)
  - Participant disconnect cleanup

This is an in-memory service — no persistence required since
presenter state is ephemeral and tied to active calls.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class PresenterRequest:
    """A request to present in a call."""

    def __init__(self, user_id: str, display_name: str = ""):
        self.user_id = user_id
        self.display_name = display_name
        self.requested_at = time.time()

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "requested_at": self.requested_at,
        }


class CallPresenterState:
    """Presenter state for a single call."""

    def __init__(self, call_id: str):
        self.call_id = call_id
        self.current_presenter: str | None = None
        self.current_presenter_name: str = ""
        self.started_at: float = 0
        self.queue: deque[PresenterRequest] = deque()
        self.viewers: set[str] = set()
        self.handoff_count: int = 0
        self.last_activity_at: float = 0

    @property
    def has_presenter(self) -> bool:
        return self.current_presenter is not None

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "current_presenter": self.current_presenter,
            "current_presenter_name": self.current_presenter_name,
            "queue": [r.to_dict() for r in self.queue],
            "queue_length": len(self.queue),
        }


class PresenterService:
    """Manages presenter state across all active calls."""

    MAX_QUEUE_SIZE = 10

    def __init__(self):
        self._calls: dict[str, CallPresenterState] = {}
        self._timeouts: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _get_or_create(self, call_id: str) -> CallPresenterState:
        if call_id not in self._calls:
            self._calls[call_id] = CallPresenterState(call_id)
        return self._calls[call_id]

    def request_presenter(
        self,
        call_id: str,
        user_id: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        """
        Request the presenter lock.

        Returns:
          - {"status": "granted"} if lock acquired
          - {"status": "queued", "position": int} if added to queue
          - {"status": "queue_full"} if queue is at max capacity
          - {"status": "denied", "reason": str} if invalid
        """
        state = self._get_or_create(call_id)

        # Already the presenter
        if state.current_presenter == user_id:
            return {"status": "granted"}

        # Already in queue
        for i, req in enumerate(state.queue):
            if req.user_id == user_id:
                return {"status": "queued", "position": i + 1}

        # No current presenter — grant immediately
        if not state.has_presenter:
            state.current_presenter = user_id
            state.current_presenter_name = display_name
            state.started_at = time.time()
            state.last_activity_at = time.time()

            logger.info(
                "presenter_granted",
                call_id=call_id,
                user_id=user_id,
            )
            return {"status": "granted"}

        # Queue is full
        if len(state.queue) >= self.MAX_QUEUE_SIZE:
            logger.warning(
                "presenter_queue_full",
                call_id=call_id,
                user_id=user_id,
            )
            return {"status": "queue_full"}

        # Someone else is presenting — add to queue
        state.queue.append(PresenterRequest(user_id, display_name))
        position = len(state.queue)

        logger.info(
            "presenter_queued",
            call_id=call_id,
            user_id=user_id,
            position=position,
        )
        return {"status": "queued", "position": position}

    async def release_presenter(
        self,
        call_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        Release the presenter lock.

        If there are queued requests, the next in line is auto-promoted.
        Returns: {"released": True, "promoted": {...} | None}
        """
        async with self._lock:
            state = self._calls.get(call_id)
            if not state:
                return {"released": False}

            promoted = None

            # Release if current presenter
            if state.current_presenter == user_id:
                state.current_presenter = None
                state.current_presenter_name = ""
                state.started_at = 0

                logger.info("presenter_released", call_id=call_id, user_id=user_id)

                # Auto-promote next in queue
                if state.queue:
                    next_req = state.queue.popleft()
                    state.current_presenter = next_req.user_id
                    state.current_presenter_name = next_req.display_name
                    state.started_at = time.time()

                    promoted = {
                        "user_id": next_req.user_id,
                        "display_name": next_req.display_name,
                    }

                    logger.info(
                        "presenter_promoted",
                        call_id=call_id,
                        user_id=next_req.user_id,
                    )
            else:
                # Not the presenter — remove from queue
                state.queue = deque(
                    r for r in state.queue if r.user_id != user_id
                )

            return {"released": True, "promoted": promoted}

    async def cancel_request(self, call_id: str, user_id: str) -> bool:
        """Remove a user from the presenter queue."""
        async with self._lock:
            state = self._calls.get(call_id)
            if not state:
                return False

            original_len = len(state.queue)
            state.queue = deque(r for r in state.queue if r.user_id != user_id)
            removed = len(state.queue) < original_len

            if removed:
                logger.info("presenter_request_cancelled", call_id=call_id, user_id=user_id)

            return removed

    async def force_stop(
        self,
        call_id: str,
        target_user_id: str,
        admin_user_id: str,
    ) -> dict[str, Any]:
        """
        Force-stop the current presenter (admin action).
        Does NOT auto-promote from queue.
        """
        async with self._lock:
            state = self._calls.get(call_id)
            if not state:
                return {"stopped": False}

            if state.current_presenter != target_user_id:
                return {"stopped": False, "reason": "User is not the current presenter"}

            state.current_presenter = None
            state.current_presenter_name = ""
            state.started_at = 0

            logger.info(
                "presenter_force_stopped",
                call_id=call_id,
                target=target_user_id,
                admin=admin_user_id,
            )

            return {"stopped": True}

    def get_state(self, call_id: str) -> dict | None:
        """Get presenter state for a call."""
        state = self._calls.get(call_id)
        return state.to_dict() if state else None

    def get_queue(self, call_id: str) -> list[dict]:
        """Get the presenter queue for a call."""
        state = self._calls.get(call_id)
        if not state:
            return []
        return [r.to_dict() for r in state.queue]

    def get_current_presenter(self, call_id: str) -> str | None:
        """Get the current presenter user_id for a call."""
        state = self._calls.get(call_id)
        return state.current_presenter if state else None

    def remove_participant(self, call_id: str, user_id: str) -> dict[str, Any]:
        """
        Handle participant leaving the call.
        Release presenter or remove from queue.
        """
        state = self._calls.get(call_id)
        if not state:
            return {"action": "none"}

        if state.current_presenter == user_id:
            return self.release_presenter(call_id, user_id)

        self.cancel_request(call_id, user_id)
        return {"action": "removed_from_queue"}

    def cleanup_call(self, call_id: str) -> None:
        """Remove all presenter state for an ended call."""
        # Cancel any active timeout for this call
        self.cancel_timeout(call_id)
        self._calls.pop(call_id, None)
        logger.info("presenter_state_cleaned", call_id=call_id)

    def cleanup_stale_calls(self, max_age_seconds: int = 14400) -> int:
        """
        Clean up calls older than max_age_seconds (default 4 hours).
        Returns the count of calls cleaned up.
        """
        now = time.time()
        stale_call_ids = []

        for call_id, state in self._calls.items():
            # Calculate age: use last_activity_at if available, else started_at
            last_activity = state.last_activity_at if state.last_activity_at else state.started_at
            if last_activity and (now - last_activity) > max_age_seconds:
                stale_call_ids.append(call_id)

        for call_id in stale_call_ids:
            self.cleanup_call(call_id)
            logger.info("presenter_stale_call_cleaned", call_id=call_id, age_seconds=max_age_seconds)

        return len(stale_call_ids)

    # ── Timeout Management ───────────────────────────────────────

    async def _timeout_handler(self, call_id: str, timeout_seconds: int) -> None:
        """
        Internal handler that waits for timeout_seconds, then auto-releases
        the current presenter if still active.
        """
        try:
            await asyncio.sleep(timeout_seconds)
            state = self._calls.get(call_id)
            if state and state.current_presenter:
                presenter_user_id = state.current_presenter
                logger.info(
                    "presenter_timeout_triggered",
                    call_id=call_id,
                    user_id=presenter_user_id,
                    timeout_seconds=timeout_seconds,
                )
                # Auto-release the presenter
                self.release_presenter(call_id, presenter_user_id)
        except asyncio.CancelledError:
            logger.debug(f"Timeout task cancelled for call {call_id}")
        except Exception as e:
            logger.error(
                "presenter_timeout_error",
                call_id=call_id,
                error=str(e),
            )

    def start_timeout(self, call_id: str, timeout_seconds: int = 300) -> None:
        """
        Start an async timeout that will auto-release the current presenter
        after timeout_seconds of inactivity.
        """
        # Cancel any existing timeout
        if call_id in self._timeouts:
            self._timeouts[call_id].cancel()

        # Create and store new timeout task
        task = asyncio.create_task(
            self._timeout_handler(call_id, timeout_seconds)
        )
        self._timeouts[call_id] = task

        logger.info(
            "presenter_timeout_started",
            call_id=call_id,
            timeout_seconds=timeout_seconds,
        )

    def cancel_timeout(self, call_id: str) -> None:
        """Cancel the timeout for a specific call."""
        task = self._timeouts.pop(call_id, None)
        if task and not task.done():
            task.cancel()
            logger.info("presenter_timeout_cancelled", call_id=call_id)

    def reset_timeout(self, call_id: str) -> None:
        """
        Reset the timeout (typically called on user activity).
        Restarts the timer with the same duration.
        """
        # For now, we'll store a default duration. In production, you may want to
        # track the original timeout_seconds per call.
        timeout_seconds = 300  # Default 5 minutes
        self.start_timeout(call_id, timeout_seconds)
        logger.debug("presenter_timeout_reset", call_id=call_id)

    # ── Presenter Handoff ────────────────────────────────────────

    async def handoff_presenter(
        self,
        call_id: str,
        from_user: str,
        to_user: str,
    ) -> dict[str, Any]:
        """
        Transfer presenter lock from one user to another, bypassing the queue.

        Validates:
          - from_user IS the current presenter
          - to_user is a valid user_id

        Returns:
          - {"status": "handoff_accepted", "new_presenter": to_user} on success
          - {"status": "error", "reason": str} on validation failure
        """
        async with self._lock:
            state = self._calls.get(call_id)
            if not state:
                return {"status": "error", "reason": "Call not found"}

            # Validate from_user is current presenter
            if state.current_presenter != from_user:
                return {
                    "status": "error",
                    "reason": f"{from_user} is not the current presenter",
                }

            if not to_user:
                return {"status": "error", "reason": "to_user is required"}

            # Perform handoff: release from_user and grant to_user
            state.current_presenter = to_user
            state.current_presenter_name = to_user  # Could look up display_name if available
            state.started_at = time.time()
            state.last_activity_at = time.time()
            state.handoff_count += 1

            # Remove to_user from queue if they were in it
            state.queue = deque(r for r in state.queue if r.user_id != to_user)

            logger.info(
                "presenter_handoff_accepted",
                call_id=call_id,
                from_user=from_user,
                to_user=to_user,
                total_handoffs=state.handoff_count,
            )

            return {
                "status": "handoff_accepted",
                "new_presenter": to_user,
                "handoff_count": state.handoff_count,
            }

    # ── Viewer Tracking ──────────────────────────────────────────

    def add_viewer(self, call_id: str, user_id: str) -> int:
        """
        Add a viewer to the call (screen share viewer, not necessarily presenting).
        Returns the current viewer count.
        """
        state = self._get_or_create(call_id)
        state.viewers.add(user_id)
        count = len(state.viewers)

        logger.debug(
            "viewer_added",
            call_id=call_id,
            user_id=user_id,
            viewer_count=count,
        )
        return count

    def remove_viewer(self, call_id: str, user_id: str) -> int:
        """
        Remove a viewer from the call.
        Returns the current viewer count.
        """
        state = self._calls.get(call_id)
        if not state:
            return 0

        state.viewers.discard(user_id)
        count = len(state.viewers)

        logger.debug(
            "viewer_removed",
            call_id=call_id,
            user_id=user_id,
            viewer_count=count,
        )
        return count

    def get_viewer_count(self, call_id: str) -> int:
        """Get the number of active viewers for a call."""
        state = self._calls.get(call_id)
        return len(state.viewers) if state else 0

    # ── Metrics & Activity ───────────────────────────────────────

    def get_presenter_metrics(self, call_id: str) -> dict[str, Any]:
        """
        Return aggregated metrics for the current presentation.

        Returns:
          - presenter_duration: seconds the current user has been presenting
          - queue_wait_times: list of wait times for queued users (seconds since request)
          - handoff_count: total number of handoffs in this session
          - viewer_count: current number of viewers
          - current_presenter: user_id or None
        """
        state = self._calls.get(call_id)
        if not state:
            return {
                "presenter_duration": 0,
                "queue_wait_times": [],
                "handoff_count": 0,
                "viewer_count": 0,
                "current_presenter": None,
            }

        # Calculate presenter duration
        presenter_duration = 0
        if state.current_presenter and state.started_at:
            presenter_duration = time.time() - state.started_at

        # Calculate queue wait times (how long each queued user has been waiting)
        queue_wait_times = [
            time.time() - req.requested_at for req in state.queue
        ]

        return {
            "presenter_duration": round(presenter_duration, 2),
            "queue_wait_times": [round(t, 2) for t in queue_wait_times],
            "handoff_count": state.handoff_count,
            "viewer_count": len(state.viewers),
            "current_presenter": state.current_presenter,
        }

    def report_activity(self, call_id: str, user_id: str) -> None:
        """
        Report activity from the current presenter (e.g., screen interaction).
        Resets the inactivity timeout and updates last_activity_at.
        """
        state = self._calls.get(call_id)
        if not state or state.current_presenter != user_id:
            return

        state.last_activity_at = time.time()
        # Reset the timeout on activity
        self.reset_timeout(call_id)

        logger.debug(
            "presenter_activity_reported",
            call_id=call_id,
            user_id=user_id,
        )


# Singleton
presenter_service = PresenterService()
