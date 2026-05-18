"""Node-lifecycle state machine.

A node moves through:

    INIT → STARTING → READY ↔ DRAINING → STOPPED

  * INIT      — process started, no services up.
  * STARTING  — services warming, registering.
  * READY     — accepting traffic.
  * DRAINING  — refusing new requests, finishing in-flight.
  * STOPPED   — clean exit.

Transitions emit ``lifecycle.{state}`` events on the distributed
event bus so other managers can react (e.g. drain → emit
``leader.relinquish``).
"""

from __future__ import annotations

import threading
import time
from enum import Enum

from app.distributed_system.distributed_events import emit


class NodeState(str, Enum):
    INIT     = "init"
    STARTING = "starting"
    READY    = "ready"
    DRAINING = "draining"
    STOPPED  = "stopped"


_VALID_TRANSITIONS = {
    NodeState.INIT:     {NodeState.STARTING, NodeState.STOPPED},
    NodeState.STARTING: {NodeState.READY, NodeState.STOPPED},
    NodeState.READY:    {NodeState.DRAINING, NodeState.STOPPED},
    NodeState.DRAINING: {NodeState.STOPPED, NodeState.READY},
    NodeState.STOPPED:  set(),
}


class NodeLifecycle:
    _singleton: "NodeLifecycle | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: NodeState = NodeState.INIT
        self._entered_at: float = time.time()

    @classmethod
    def instance(cls) -> "NodeLifecycle":
        if cls._singleton is None:
            cls._singleton = NodeLifecycle()
        return cls._singleton

    def state(self) -> NodeState:
        with self._lock:
            return self._state

    def transition(self, target: NodeState) -> bool:
        with self._lock:
            if target == self._state:
                return True
            if target not in _VALID_TRANSITIONS.get(self._state, set()):
                return False
            self._state = target
            self._entered_at = time.time()
        emit(f"lifecycle.{target.value}", {"entered_at": self._entered_at})
        return True

    def is_ready(self) -> bool:
        return self.state() is NodeState.READY

    def is_running(self) -> bool:
        return self.state() in (NodeState.READY, NodeState.DRAINING)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state":         self._state.value,
                "entered_at":    self._entered_at,
                "uptime_in_state_sec": round(time.time() - self._entered_at, 1),
            }


def get_lifecycle() -> NodeLifecycle:
    return NodeLifecycle.instance()
