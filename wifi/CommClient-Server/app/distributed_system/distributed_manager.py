"""Distributed manager — top-level lifecycle for the package.

Starts/stops the per-manager loops in the right order:

    1. NodeLifecycle.transition(STARTING)
    2. ClusterMembership.start()
    3. HeartbeatManager.start()
    4. RecoveryManager.start()
    5. NodeLifecycle.transition(READY)

Stop order is the reverse, finishing with ``STOPPED``.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.distributed_system.cluster_membership import get_cluster_membership
from app.distributed_system.cluster_manager import get_cluster_manager
from app.distributed_system.distributed_events import emit, history
from app.distributed_system.heartbeat_manager import get_heartbeat_manager
from app.distributed_system.node_lifecycle import NodeState, get_lifecycle
from app.distributed_system.recovery_manager import get_recovery_manager

logger = get_logger(__name__)


class DistributedManager:
    _singleton: "DistributedManager | None" = None

    def __init__(self) -> None:
        self._started = False

    @classmethod
    def instance(cls) -> "DistributedManager":
        if cls._singleton is None:
            cls._singleton = DistributedManager()
        return cls._singleton

    def start(self) -> None:
        if self._started:
            return
        lc = get_lifecycle()
        lc.transition(NodeState.STARTING)

        get_cluster_membership().start()
        get_heartbeat_manager().start()
        get_recovery_manager().start()

        lc.transition(NodeState.READY)
        self._started = True
        emit("ds.started", {"members": len(get_cluster_manager().members())})
        logger.info("distributed_system_started")

    def stop(self) -> None:
        if not self._started:
            return
        lc = get_lifecycle()
        lc.transition(NodeState.DRAINING)

        get_recovery_manager().stop()
        get_heartbeat_manager().stop()
        get_cluster_membership().stop()

        lc.transition(NodeState.STOPPED)
        self._started = False
        emit("ds.stopped", {})
        logger.info("distributed_system_stopped")

    def snapshot(self) -> dict:
        return {
            "started": self._started,
            "cluster": get_cluster_manager().snapshot(),
            "events":  history(limit=50),
        }


def get_distributed_manager() -> DistributedManager:
    return DistributedManager.instance()


def start_distributed_system() -> None:
    get_distributed_manager().start()


def stop_distributed_system() -> None:
    get_distributed_manager().stop()
