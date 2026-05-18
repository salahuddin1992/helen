"""Consensus manager — sloppy-quorum write/read primitive.

Wraps ``services.quorum_decision`` to expose a higher-level
``ConsensusManager`` interface that:

  * Records every consensus write in the audit chain.
  * Translates QuorumLost into a typed exception subclass so callers
    can act on it.
  * Emits ``consensus.{accepted,failed}`` events.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.distributed_system.distributed_config import get_config
from app.distributed_system.distributed_events import emit
from app.distributed_system.distributed_exceptions import (
    ConsensusError, QuorumLostError,
)

logger = get_logger(__name__)


class ConsensusManager:
    _singleton: "ConsensusManager | None" = None

    def __init__(self) -> None:
        self._writes_total = 0
        self._writes_accepted = 0
        self._writes_rejected = 0

    @classmethod
    def instance(cls) -> "ConsensusManager":
        if cls._singleton is None:
            cls._singleton = ConsensusManager()
        return cls._singleton

    async def write(
        self,
        kind: str,
        key: str,
        value: Any,
        *,
        replication: Optional[int] = None,
        required_acks: Optional[int] = None,
        timeout: Optional[float] = None,
        audit: bool = True,
    ) -> dict:
        """Cluster-wide consensus write. Raises QuorumLostError on
        insufficient acks. Returns the result dict on success."""
        cfg = get_config()
        if not cfg.enable_consensus:
            raise ConsensusError("consensus disabled by config")
        try:
            from app.services.quorum_decision import quorum_write
        except ImportError as e:
            raise ConsensusError(f"quorum_decision missing: {e}")

        result = await quorum_write(
            kind=kind, key=key, value=value,
            replication=replication or cfg.replication_factor,
            required_acks=required_acks,
            timeout=timeout or cfg.quorum_timeout_sec,
        )
        self._writes_total += 1

        if not result.accepted:
            self._writes_rejected += 1
            emit("consensus.failed", {
                "kind": kind, "key": key,
                "acks": result.acks_received, "needed": result.acks_required,
                "failures": result.failures[:5],
            })
            raise QuorumLostError(
                f"acks={result.acks_received} needed={result.acks_required}"
            )

        self._writes_accepted += 1

        if audit:
            try:
                from app.services.audit_replication import get_audit_replicator
                get_audit_replicator().append_local(
                    event="consensus.write",
                    actor="distributed_system",
                    payload={
                        "kind": kind, "key": key,
                        "acks": result.acks_received,
                        "duration_ms": result.duration_ms,
                    },
                )
            except Exception:
                pass

        emit("consensus.accepted", {
            "kind": kind, "key": key,
            "acks": result.acks_received,
            "duration_ms": result.duration_ms,
        })
        return {
            "accepted":      True,
            "acks_received": result.acks_received,
            "acks_required": result.acks_required,
            "duration_ms":   result.duration_ms,
        }

    async def read(
        self,
        kind: str,
        key: str,
        *,
        replication: Optional[int] = None,
        required_acks: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Optional[dict]:
        cfg = get_config()
        try:
            from app.services.quorum_decision import quorum_read
        except ImportError as e:
            raise ConsensusError(f"quorum_decision missing: {e}")
        return await quorum_read(
            kind=kind, key=key,
            replication=replication or cfg.replication_factor,
            required_acks=required_acks,
            timeout=timeout or cfg.quorum_timeout_sec,
        )

    def stats(self) -> dict:
        return {
            "writes_total":     self._writes_total,
            "writes_accepted":  self._writes_accepted,
            "writes_rejected":  self._writes_rejected,
        }


def get_consensus_manager() -> ConsensusManager:
    return ConsensusManager.instance()
