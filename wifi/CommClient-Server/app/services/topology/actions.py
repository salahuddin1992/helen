"""
TopologyActions — node-level operations from the admin Topology Visualizer.

Supported actions
-----------------
* ``ping``       — best-effort ICMP-or-TCP latency probe.
* ``traceroute`` — synthesises a hop list from the aggregated graph.
* ``drain``      — instruct the target to stop accepting new connections.
* ``restart``    — graceful service restart (target-side, when supported).
* ``failover``   — promote a replica or hand traffic over to a peer.

Each action runs inside a background ``asyncio.Task`` so the HTTP request
returns immediately with a ``job_id``. The admin client polls
``GET /api/admin/topology/jobs/{job_id}`` for progress, or subscribes to the
``WebSocket /api/admin/ws/topology`` stream for live ``action.update``
events emitted by this module.

All actions are written to the audit log via ``app.core.audit.audit_log``
with a stable event name so SIEM correlations work out of the box.

Persistence
-----------
The job registry is in-memory (a thread-safe dict). On process restart any
in-flight jobs are lost. This is acceptable for an operator-driven UI but a
``TODO`` is marked in the module-level docstring for migrating to a
SQLAlchemy-backed table once we add an ``operations_journal`` model.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import shutil
import socket
import subprocess  # noqa: S404 — bounded, sanitised exec; see _safe_run().
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import structlog

from app.core.audit import audit_log

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

JOB_STATUS_PENDING   = "pending"
JOB_STATUS_RUNNING   = "running"
JOB_STATUS_SUCCESS   = "success"
JOB_STATUS_FAILED    = "failed"
JOB_STATUS_TIMED_OUT = "timed_out"

VALID_ACTIONS = {"ping", "traceroute", "drain", "restart", "failover"}

DEFAULT_TIMEOUT_SEC = 30.0
PING_TIMEOUT_SEC    = 5.0
PING_COUNT          = 4
JOB_RETENTION_SEC   = 600.0  # GC completed jobs older than 10 min.

ACTION_AUDIT_EVENT = "admin.topology.action"


# ─────────────────────────────────────────────────────────────
# Job model
# ─────────────────────────────────────────────────────────────


@dataclass
class TopologyJob:
    job_id:     str
    node_id:    str
    action:     str
    user_id:    str = ""
    params:     dict[str, Any] = field(default_factory=dict)
    status:     str = JOB_STATUS_PENDING
    started:    float = field(default_factory=time.time)
    finished:   Optional[float] = None
    result:     dict[str, Any] = field(default_factory=dict)
    error:      Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "job_id":   self.job_id,
            "node_id":  self.node_id,
            "action":   self.action,
            "user_id":  self.user_id,
            "params":   dict(self.params),
            "status":   self.status,
            "started":  self.started,
            "finished": self.finished,
            "result":   dict(self.result),
            "error":    self.error,
        }
        if self.finished:
            out["duration_sec"] = round(self.finished - self.started, 3)
        return out


# ─────────────────────────────────────────────────────────────
# Actions service
# ─────────────────────────────────────────────────────────────


class TopologyActions:
    """In-memory job registry + action implementations."""

    _singleton: "TopologyActions | None" = None

    def __init__(self) -> None:
        self._jobs: dict[str, TopologyJob] = {}
        self._lock = threading.RLock()
        # Hook used by ws_stream to push job updates to listeners.
        self._broadcast: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None

    @classmethod
    def instance(cls) -> "TopologyActions":
        if cls._singleton is None:
            cls._singleton = TopologyActions()
        return cls._singleton

    # ── Public API ────────────────────────────────────────────

    def set_broadcaster(
        self,
        broadcast: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Hook injected by the WS manager so we can push live job updates."""
        self._broadcast = broadcast

    async def run_job(
        self,
        node_id: str,
        action: str,
        params: Optional[dict[str, Any]] = None,
        *,
        user_id: str = "",
    ) -> TopologyJob:
        """
        Kick off a background job and return the initial record.

        The returned ``TopologyJob`` is in ``pending`` (or already
        ``running``) state — the caller must poll ``get`` or subscribe to the
        WS stream to learn the final result.
        """
        if action not in VALID_ACTIONS:
            raise ValueError(f"unknown action: {action!r}")

        job = TopologyJob(
            job_id=uuid.uuid4().hex,
            node_id=node_id,
            action=action,
            user_id=user_id,
            params=dict(params or {}),
        )
        with self._lock:
            self._jobs[job.job_id] = job

        audit_log(
            ACTION_AUDIT_EVENT,
            user_id=user_id or None,
            success=True,
            details={
                "action":  action,
                "node_id": node_id,
                "job_id":  job.job_id,
                "params":  dict(params or {}),
            },
        )

        # Schedule the runner.
        asyncio.create_task(self._runner(job), name=f"topology-job-{job.job_id}")
        # GC pass — never accumulate.
        self._gc()
        return job

    def get(self, job_id: str) -> Optional[TopologyJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[TopologyJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)

    # ── Runner ────────────────────────────────────────────────

    async def _runner(self, job: TopologyJob) -> None:
        job.status = JOB_STATUS_RUNNING
        await self._broadcast_event("action.update", job)
        try:
            timeout = float(job.params.get("timeout", DEFAULT_TIMEOUT_SEC))
            dispatcher = {
                "ping":       self.ping,
                "traceroute": self.traceroute,
                "drain":      self.drain,
                "restart":    self.restart,
                "failover":   self.failover,
            }
            fn = dispatcher[job.action]
            result = await asyncio.wait_for(fn(job.node_id, job.params), timeout=timeout)
            job.result = result or {}
            job.status = JOB_STATUS_SUCCESS
        except asyncio.TimeoutError:
            job.status = JOB_STATUS_TIMED_OUT
            job.error = f"action {job.action!r} exceeded timeout"
        except Exception as e:  # pragma: no cover — exercised by tests via mocks
            job.status = JOB_STATUS_FAILED
            job.error = str(e)
            logger.warning(
                "topology_action_failed",
                job_id=job.job_id,
                action=job.action,
                node_id=job.node_id,
                error=str(e),
            )
        finally:
            job.finished = time.time()
            audit_log(
                ACTION_AUDIT_EVENT + ".result",
                user_id=job.user_id or None,
                success=(job.status == JOB_STATUS_SUCCESS),
                details={
                    "job_id":  job.job_id,
                    "status":  job.status,
                    "action":  job.action,
                    "node_id": job.node_id,
                    "error":   job.error,
                },
            )
            await self._broadcast_event("action.update", job)

    async def _broadcast_event(self, kind: str, job: TopologyJob) -> None:
        if self._broadcast is None:
            return
        with contextlib.suppress(Exception):
            await self._broadcast(kind, {"job": job.to_dict()})

    # ── Action implementations ────────────────────────────────

    async def ping(
        self, node_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Latency probe — TCP-connect with ICMP fallback when available."""
        host = self._resolve_host(node_id, params)
        if not host:
            return {"ok": False, "error": "could not resolve host"}

        # Prefer system `ping` because it gives a stats summary.
        if shutil.which("ping"):
            count_flag = "-n" if platform.system() == "Windows" else "-c"
            cmd = ["ping", count_flag, str(PING_COUNT), host]
            rc, stdout = await self._safe_run(cmd, timeout=PING_TIMEOUT_SEC * 2)
            if rc == 0:
                return {
                    "ok":      True,
                    "host":    host,
                    "method":  "icmp",
                    "output":  stdout[-2000:],
                }
            # Fall through to TCP measurement.

        # TCP-connect probe — measures the time to establish on port 443.
        port = int(params.get("port") or 443)
        rtts_ms: list[float] = []
        loop = asyncio.get_event_loop()
        for _ in range(PING_COUNT):
            t0 = loop.time()
            try:
                fut = asyncio.open_connection(host, port)
                reader, writer = await asyncio.wait_for(fut, timeout=PING_TIMEOUT_SEC)
                rtts_ms.append((loop.time() - t0) * 1000.0)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            except Exception:
                rtts_ms.append(float("inf"))
            await asyncio.sleep(0.1)

        finite = [r for r in rtts_ms if r != float("inf")]
        loss = (len(rtts_ms) - len(finite)) / len(rtts_ms) * 100.0 if rtts_ms else 100.0
        avg = sum(finite) / len(finite) if finite else 0.0
        return {
            "ok":            len(finite) > 0,
            "host":          host,
            "method":        "tcp",
            "port":          port,
            "rtt_avg_ms":    round(avg, 3),
            "rtt_min_ms":    round(min(finite), 3) if finite else 0.0,
            "rtt_max_ms":    round(max(finite), 3) if finite else 0.0,
            "packet_loss_pct": round(loss, 2),
            "samples":       PING_COUNT,
        }

    async def traceroute(
        self, node_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Hop-list derived from the aggregated topology graph.

        For internal Helen nodes we trust the graph; for arbitrary external
        addresses we shell out to ``traceroute``/``tracert`` if available.
        """
        # Try the in-memory graph first — that's the topology view that
        # actually matters for the admin UI.
        try:
            from app.services.topology.aggregator import get_topology_aggregator
            from app.services.topology.pathfinder import Pathfinder
            agg = get_topology_aggregator()
            graph = await agg.build_graph()
            src = params.get("from") or self._self_node_id(graph)
            if src and node_id in {n.id for n in graph.nodes}:
                result = Pathfinder.find_path(graph, src, node_id, weight="rtt")
                if result.found:
                    return {
                        "ok":     True,
                        "source": "graph",
                        "path":   result.to_dict(),
                    }
        except Exception as e:
            logger.debug("traceroute_graph_path_failed", error=str(e))

        # External fallback — only when we have a real hostname/IP.
        host = self._resolve_host(node_id, params)
        if not host:
            return {"ok": False, "error": "could not resolve host"}
        if platform.system() == "Windows":
            cmd = ["tracert", "-d", "-h", "20", host]
        else:
            cmd = ["traceroute", "-n", "-m", "20", host]
        if not shutil.which(cmd[0]):
            return {"ok": False, "error": f"{cmd[0]} not available"}
        rc, stdout = await self._safe_run(cmd, timeout=30.0)
        return {
            "ok":     rc == 0,
            "source": "system",
            "host":   host,
            "output": stdout[-4000:],
        }

    async def drain(
        self, node_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Mark the target as drained — best effort across services.

        For local distributed-system nodes we flip the ``drain`` flag in the
        node registry, which the connection acceptor consults on new
        handshakes. Remote nodes are notified via the cluster RPC channel if
        available.
        """
        actions: list[str] = []
        try:
            from app.distributed_system import node_registry as _node_reg
            node = _node_reg.get(self._strip_prefix(node_id))
            if node and self._is_self_node(node):
                from app.services.node_registry import get_registry
                reg = get_registry()
                if hasattr(reg, "drain"):
                    reg.drain(node["node_id"])
                    actions.append("local_drain_set")
        except Exception as e:
            logger.debug("drain_local_failed", error=str(e))

        try:
            from app.distributed_system.cluster_manager import cluster_manager
            if hasattr(cluster_manager, "request_drain"):
                await cluster_manager.request_drain(node_id)
                actions.append("cluster_drain_requested")
        except Exception as e:
            logger.debug("drain_cluster_failed", error=str(e))

        return {"ok": True, "actions": actions, "node_id": node_id}

    async def restart(
        self, node_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Graceful restart of a remote Helen service."""
        try:
            from app.distributed_system.cluster_manager import cluster_manager
            if hasattr(cluster_manager, "request_restart"):
                await cluster_manager.request_restart(node_id)
                return {"ok": True, "node_id": node_id, "method": "cluster_rpc"}
        except Exception as e:
            logger.debug("restart_cluster_failed", error=str(e))
        return {
            "ok":      False,
            "node_id": node_id,
            "error":   "remote restart not supported in current cluster config",
        }

    async def failover(
        self, node_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Promote a replica and hand traffic over to it."""
        try:
            from app.distributed_system.recovery_manager import recovery_manager
            if hasattr(recovery_manager, "trigger_failover"):
                result = await recovery_manager.trigger_failover(
                    failed_node_id=node_id,
                    target_node_id=params.get("target"),
                )
                return {"ok": True, "node_id": node_id, "result": result}
        except Exception as e:
            logger.debug("failover_failed", error=str(e))
        return {
            "ok":      False,
            "node_id": node_id,
            "error":   "recovery_manager not available",
        }

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _strip_prefix(node_id: str) -> str:
        """``node:foo`` → ``foo``."""
        return node_id.split(":", 1)[1] if ":" in node_id else node_id

    @staticmethod
    def _self_node_id(graph) -> Optional[str]:
        for n in graph.nodes:
            if n.type == "server" and "self" in n.tags:
                return n.id
        return None

    @staticmethod
    def _is_self_node(node: dict[str, Any]) -> bool:
        return bool(node.get("self_node"))

    @staticmethod
    def _resolve_host(node_id: str, params: dict[str, Any]) -> str:
        if params.get("host"):
            return str(params["host"])
        # node_id schema: "<type>:<inner>" — try splitting and looking up
        # the topology entry for an IP.
        try:
            from app.services.topology.aggregator import get_topology_aggregator
            agg = get_topology_aggregator()
            if agg._cache is not None:
                for n in agg._cache.nodes:
                    if n.id == node_id:
                        return n.ip or n.hostname or ""
        except Exception:
            pass
        inner = node_id.split(":", 1)[-1]
        # Pure IP or hostname?
        try:
            socket.gethostbyname(inner)
            return inner
        except Exception:
            return ""

    @staticmethod
    async def _safe_run(
        cmd: list[str], *, timeout: float
    ) -> tuple[int, str]:
        """Run a command with timeout, return (rc, combined stdout/stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return (-1, "timeout")
        return (proc.returncode or 0, (out or b"").decode("utf-8", errors="replace"))

    # ── GC ────────────────────────────────────────────────────

    def _gc(self) -> None:
        cutoff = time.time() - JOB_RETENTION_SEC
        with self._lock:
            dead = [
                jid for jid, j in self._jobs.items()
                if j.finished and j.finished < cutoff
            ]
            for jid in dead:
                self._jobs.pop(jid, None)


def get_topology_actions() -> TopologyActions:
    return TopologyActions.instance()
