"""ServiceDiscoveryManager — top-level lifecycle orchestrator.

Composes:

  * ServiceRegistry         (in-memory + JSON persistence)
  * StaleReaper             (TTL eviction loop)
  * Self-registration       (Helen-Server registers its own services
                             at startup)
  * Periodic persist        (flush every 30 s)

Self-registration runs at startup and announces this Helen-Server
as both PEER and (if applicable) RELAY/SIGNALING/MEDIA_GATEWAY/
DISCOVERY based on the role flags from ``services.node_registry``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from app.core.logging import get_logger

from app.service_discovery.discovery_config import get_config
from app.service_discovery.discovery_events import emit, history
from app.service_discovery.region_zone import my_region, my_zone
from app.service_discovery.service_record import (
    ServiceRecord, ServiceStatus, ServiceType,
)
from app.service_discovery.service_registry import get_registry
from app.service_discovery.service_signing import sign_record, status as signing_status
from app.service_discovery import stale_reaper

logger = get_logger(__name__)


_SELF_REGISTRATION_INTERVAL_SEC = 30.0


def _self_records() -> list[ServiceRecord]:
    """Build records for every role this Helen-Server is hosting."""
    out: list[ServiceRecord] = []
    try:
        from app.services.node_registry import get_registry as get_nr
        from app.core.config import get_settings
        nr = get_nr()
        self_node = next(
            (n for n in nr.nodes(include_dead=True) if n.self_node),
            None,
        )
        if self_node is None:
            return out
        cluster_id = get_settings().COMMCLIENT_CLUSTER_ID or "default"
        cap = self_node.capacity
        load = self_node.load

        # Always: PEER role.
        out.append(ServiceRecord(
            service_id=f"peer:{self_node.node_id}",
            service_type=ServiceType.PEER,
            server_id=self_node.node_id,
            host=self_node.host,
            port=self_node.port,
            protocol="http",
            cluster_id=cluster_id,
            region=my_region(),
            zone=my_zone(),
            status=ServiceStatus.HEALTHY,
            ttl_sec=get_config().default_ttl_sec,
            max_capacity=cap.max_concurrent_sockets,
            current_load=load.active_sockets,
            capabilities={
                "cores":   self_node.capability.cpu_cores,
                "ram_gb":  self_node.capability.ram_gb,
                "version": self_node.capability.version,
            },
        ))

        # Conditional roles (relay / sfu / messaging / file_transfer / etc).
        roles = self_node.roles
        role_to_type = [
            (roles.relay,         ServiceType.RELAY),
            (roles.signaling,     ServiceType.SIGNALING),
            (roles.sfu,           ServiceType.MEDIA_GATEWAY),
            (roles.file_transfer, ServiceType.STORAGE),
            (roles.messaging,     ServiceType.PEER),  # already covered
        ]
        for enabled, st in role_to_type:
            if not enabled or st == ServiceType.PEER:
                continue
            out.append(ServiceRecord(
                service_id=f"{st.value}:{self_node.node_id}",
                service_type=st,
                server_id=self_node.node_id,
                host=self_node.host,
                port=self_node.port,
                protocol="http",
                cluster_id=cluster_id,
                region=my_region(),
                zone=my_zone(),
                status=ServiceStatus.HEALTHY,
                ttl_sec=get_config().default_ttl_sec,
                max_capacity=(
                    cap.max_audio_participants if st == ServiceType.MEDIA_GATEWAY
                    else cap.max_concurrent_sockets
                ),
                current_load=load.active_sockets,
                capabilities={
                    "cores":   self_node.capability.cpu_cores,
                    "version": self_node.capability.version,
                },
            ))

        # Sign each record with the cluster secret.
        for r in out:
            sign_record(r)
        return out
    except Exception as e:
        logger.warning("sd_self_records_failed", error=str(e))
        return out


class ServiceDiscoveryManager:
    _singleton: "ServiceDiscoveryManager | None" = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._cycles = 0

    @classmethod
    def instance(cls) -> "ServiceDiscoveryManager":
        if cls._singleton is None:
            cls._singleton = ServiceDiscoveryManager()
        return cls._singleton

    # ── Background loop ───────────────────────────────────

    async def _self_register_cycle(self) -> int:
        """Re-announce self records every cycle. Acts as our own
        heartbeat."""
        records = _self_records()
        reg = get_registry()
        for r in records:
            try:
                reg.register(r, verify_signature=True)
            except Exception as e:
                logger.warning("sd_self_register_failed",
                               type=r.service_type.value, error=str(e)[:80])
        if records:
            reg.persist_if_dirty()
        return len(records)

    async def _run_loop(self) -> None:
        self._running = True
        logger.info("sd_manager_started",
                    interval_sec=_SELF_REGISTRATION_INTERVAL_SEC)
        try:
            while self._running:
                try:
                    await self._self_register_cycle()
                except Exception as e:
                    logger.warning("sd_cycle_failed", error=str(e))
                self._cycles += 1
                await asyncio.sleep(_SELF_REGISTRATION_INTERVAL_SEC)
        finally:
            logger.info("sd_manager_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            stale_reaper.start()
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="sd-manager",
            )
        except RuntimeError:
            logger.warning("sd_manager_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        stale_reaper.stop()
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ──────────────────────────────────────

    def snapshot(self) -> dict:
        cfg = get_config()
        return {
            "running":   self._running,
            "cycles":    self._cycles,
            "config": {
                "default_ttl_sec":      cfg.default_ttl_sec,
                "heartbeat_grace_sec":  cfg.heartbeat_grace_sec,
                "reaper_interval_sec":  cfg.reaper_interval_sec,
                "min_health_score":     cfg.min_health_score,
                "self_region":          cfg.self_region,
                "self_zone":            cfg.self_zone,
                "enable_federation_lookup": cfg.enable_federation_lookup,
            },
            "signing":   signing_status(),
            "registry":  get_registry().stats(),
            "reaper":    stale_reaper.stats(),
            "events":    history(limit=50),
        }


def get_discovery_manager() -> ServiceDiscoveryManager:
    return ServiceDiscoveryManager.instance()


def start_discovery() -> None:
    get_discovery_manager().start()


def stop_discovery() -> None:
    get_discovery_manager().stop()
