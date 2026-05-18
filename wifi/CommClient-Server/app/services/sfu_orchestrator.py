"""
SFU orchestrator — automatic mesh ↔ SFU topology switching.

The desktop client has a hard mesh cap of 8 peers (each peer keeps
N-1 PeerConnections; cost grows quadratically). When a 9th
participant tries to join, that 8-peer mesh would either drop the
new joiner or melt under bandwidth pressure.

This service watches every active call and emits a topology-switch
event the moment a call crosses ``MESH_UPGRADE_AT`` participants.
The desktop client listens for ``call:topology_change`` and runs
the mediasoup-client adapter that's already wired in
``MediasoupSFUAdapter.ts``. The reverse switch (SFU → mesh) fires
when the call falls below ``MESH_DOWNGRADE_AT`` to free the SFU's
CPU/bandwidth budget for the next big call.

Hysteresis (upgrade-at 7, downgrade-at 4) prevents flapping when a
borderline group call has someone repeatedly joining/leaving.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class CallTopologyState:
    call_id: str
    topology: str = "mesh"            # mesh | sfu
    participant_count: int = 0
    upgraded_at: float = 0.0
    downgraded_at: float = 0.0
    last_change_reason: str = ""


class SFUOrchestrator:
    """Per-call topology decision-maker.

    Wire-up
    -------
    From the call lifecycle handlers (call_join_group / call_leave_group):

        await orchestrator.observe_participant_count(call_id, n)

    The orchestrator returns the new topology (or None if no change).
    When it changes the topology it ALSO fires ``broadcast_event(...)``
    so the call_handlers module can re-broadcast the change to every
    participant via Socket.IO.
    """

    # Hysteresis thresholds
    MESH_UPGRADE_AT = 7        # 7+ participants → switch to SFU
    MESH_DOWNGRADE_AT = 4      # ≤4 participants → back to mesh
    UPGRADE_DEBOUNCE_SEC = 5.0  # don't flip-flop within 5 s

    def __init__(
        self,
        broadcast_event: Optional[Callable] = None,
    ) -> None:
        # broadcast_event(call_id: str, event: str, payload: dict)
        self.broadcast = broadcast_event
        self._states: dict[str, CallTopologyState] = {}
        self._lock = asyncio.Lock()

    async def observe_participant_count(
        self, call_id: str, count: int,
    ) -> Optional[str]:
        """Update the participant count for a call, return the new
        topology if a switch happened, else None."""
        async with self._lock:
            st = self._states.setdefault(call_id, CallTopologyState(call_id))
            st.participant_count = count
            now = time.time()

            new_topology: Optional[str] = None
            if (st.topology == "mesh"
                    and count >= self.MESH_UPGRADE_AT
                    and now - st.upgraded_at > self.UPGRADE_DEBOUNCE_SEC):
                new_topology = "sfu"
                st.topology = "sfu"
                st.upgraded_at = now
                st.last_change_reason = (
                    f"participant_count={count} crossed upgrade threshold"
                )
            elif (st.topology == "sfu"
                    and count <= self.MESH_DOWNGRADE_AT
                    and now - st.downgraded_at > self.UPGRADE_DEBOUNCE_SEC):
                new_topology = "mesh"
                st.topology = "mesh"
                st.downgraded_at = now
                st.last_change_reason = (
                    f"participant_count={count} below downgrade threshold"
                )

            if new_topology and self.broadcast:
                try:
                    await self._fire_event(call_id, new_topology, count)
                except Exception as exc:
                    logger.warning("sfu_orchestrator_broadcast_failed",
                                   call_id=call_id, error=str(exc))

            if new_topology:
                logger.info("call_topology_changed",
                            call_id=call_id, new=new_topology,
                            participants=count)
            return new_topology

    async def _fire_event(self, call_id: str, topology: str,
                           count: int) -> None:
        payload = {
            "call_id": call_id,
            "topology": topology,
            "participant_count": count,
            "changed_at": time.time(),
        }
        result = self.broadcast(call_id, "call:topology_change", payload)
        if asyncio.iscoroutine(result):
            await result

    async def topology_for(self, call_id: str) -> str:
        async with self._lock:
            st = self._states.get(call_id)
            return st.topology if st else "mesh"

    async def remove(self, call_id: str) -> None:
        async with self._lock:
            self._states.pop(call_id, None)

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "calls_tracked": len(self._states),
                "by_topology": {
                    "mesh": sum(1 for s in self._states.values()
                                 if s.topology == "mesh"),
                    "sfu": sum(1 for s in self._states.values()
                                if s.topology == "sfu"),
                },
                "calls": [
                    {
                        "call_id": s.call_id,
                        "topology": s.topology,
                        "participants": s.participant_count,
                        "last_change_reason": s.last_change_reason,
                    }
                    for s in self._states.values()
                ],
            }


# Module-level singleton — wired from call_handlers when needed
_INSTANCE: Optional[SFUOrchestrator] = None


def get_orchestrator() -> SFUOrchestrator:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SFUOrchestrator()
    return _INSTANCE


def set_broadcaster(fn: Callable) -> None:
    """Late-bind the Socket.IO broadcaster. Called from app.main during
    startup once the call_handlers module is imported."""
    get_orchestrator().broadcast = fn
