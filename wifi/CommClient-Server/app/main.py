"""
FastAPI application factory + Socket.IO integration.
Entry point for the CommClient server.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.db.base import Base
from app.db.session import engine
from app.services.discovery_service import mdns_service, udp_broadcast
from app.services.peer_registry import udp_listener_service

# Import all models so SQLAlchemy knows about them
from app.models import *  # noqa: F401, F403

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle — startup and shutdown hooks."""
    # ── Startup ─────────────────────────────────────────
    setup_logging()
    logger.info("server_starting", host=settings.HOST, port=settings.PORT)

    # Crash reporter — install excepthook so unhandled exceptions land
    # in a local SQLite store (admin can list via /api/admin/crashes).
    # Stays 100 % LAN-internal — no external telemetry.
    try:
        from pathlib import Path as _CrashPath
        from app.services.crash_reporter import install_crash_reporter
        _crash_db_dir = _CrashPath(settings.SQLITE_PATH).resolve().parent
        if not _crash_db_dir.is_absolute():
            _crash_db_dir = (settings.PROJECT_ROOT / _crash_db_dir).resolve()
        _crash_db_dir.mkdir(parents=True, exist_ok=True)
        install_crash_reporter(
            data_dir=str(_crash_db_dir),
            helen_version=getattr(settings, "VERSION", "1.0.0"),
        )
        logger.info("crash_reporter_installed",
                    data_dir=str(_crash_db_dir))
    except Exception as _ce:
        logger.warning("crash_reporter_install_failed", error=str(_ce))

    # Audit chain — tamper-evident hash chain mirroring every
    # audit_log() call. Lives in a separate SQLite so an attacker
    # rewriting the audit_logs table can be detected by chain.verify().
    try:
        from pathlib import Path as _ChainPath
        from app.services.audit_chain import configure_audit_chain
        _chain_db_dir = _ChainPath(settings.SQLITE_PATH).resolve().parent
        if not _chain_db_dir.is_absolute():
            _chain_db_dir = (settings.PROJECT_ROOT / _chain_db_dir).resolve()
        _chain_db_dir.mkdir(parents=True, exist_ok=True)
        configure_audit_chain(str(_chain_db_dir / "audit_chain.db"))
        logger.info("audit_chain_configured",
                    db=str(_chain_db_dir / "audit_chain.db"))
    except Exception as _ae:
        logger.warning("audit_chain_configure_failed", error=str(_ae))

    # SECURITY: Refuse to boot with a weak or placeholder JWT_SECRET.
    _WEAK_JWT_SECRETS = {
        "commclient-lan-secret-CHANGE-ME",
        "change-me",
        "secret",
        "changeme",
        # Old NSIS installer fallback that defaulted to the same
        # value across every install. Caught here so a server using
        # this exact value refuses to boot.
        "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9",
        # Last-ditch installer placeholder — designed to be obvious.
        "REPLACE_ME_BEFORE_RUNNING_HELEN_SERVER_64_chars_long_xxxxxxxxxx",
    }
    if (
        settings.JWT_SECRET in _WEAK_JWT_SECRETS
        or len(settings.JWT_SECRET) < 32
    ):
        logger.error(
            "jwt_secret_insecure",
            message="JWT_SECRET is missing, too short, or set to a known "
                    "placeholder. Refusing to start. Generate one with: "
                    "python -c \"import secrets; print(secrets.token_hex(32))\" "
                    "and set it in .env.",
        )
        raise RuntimeError("Insecure JWT_SECRET — refusing to start.")

    # ── Migrations ──
    # 1) Run Alembic to head FIRST so any structural changes land before the
    #    fall-back create_all runs. Idempotent on a fresh DB.
    # 2) `Base.metadata.create_all` then fills in anything Alembic hasn't yet
    #    been tracked for (the legacy pre-Alembic tables).
    # 3) Lightweight column-add migrations for older databases shipped
    #    before individual columns existed.
    try:
        from app.db.alembic_runner import run_alembic_upgrade
        await run_alembic_upgrade(str(engine.url))
    except Exception as _e:
        logger.error("alembic_runner_invocation_failed", error=str(_e))

    # Create database tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Apply lightweight column-add migrations for pre-existing DBs
    from app.db.startup_migrations import run_startup_migrations
    await run_startup_migrations(engine)

    logger.info("database_ready", backend=settings.DB_BACKEND)

    # Windows Firewall rules — idempotent inbound-allow provisioning.
    # No-op when we're not running as administrator; logs a hint if
    # rules are missing and we can't add them.
    try:
        from app.services.firewall_provision import ensure_firewall_rules
        fw_summary = ensure_firewall_rules()
        if fw_summary.get("added"):
            logger.info("firewall_rules_provisioned", **fw_summary)
    except Exception as _e:
        logger.debug("firewall_provision_failed", error=str(_e))

    # Start LAN discovery services — but skip them when the deployment
    # mandates router-only traffic. With HELEN_REQUIRE_ROUTER=1 the only
    # legitimate path to this server is via Helen-Router; advertising on
    # mDNS / UDP broadcast would let clients believe they can connect
    # directly (they can't — RouterRequiredMiddleware would 403 them).
    _disable_broadcast = os.environ.get("HELEN_DISABLE_BROADCAST", "").lower() in ("1", "true", "yes")
    _require_router = os.environ.get("HELEN_REQUIRE_ROUTER", "").lower() in ("1", "true", "yes")
    if _disable_broadcast or _require_router:
        logger.info(
            "discovery_disabled",
            reason="router_required" if _require_router else "explicit_disable",
            mdns_skipped=True,
            udp_broadcast_skipped=True,
        )
    else:
        await udp_broadcast.start()
        await mdns_service.start()
        await udp_listener_service.start()

    # Auto-register with Helen-Router — best effort, no-op if
    # HELEN_ROUTER_URL is not configured. Picks up HELEN_ROUTER_TOKEN
    # from the environment so the same secret used by RouterRequiredMiddleware
    # also authorises our /router/register call.
    try:
        from app.services.router_client import maybe_start_router_client
        await maybe_start_router_client()
    except Exception as _e:
        logger.warning("router_client_start_failed", error=str(_e))

    # ── Distributed-transformation services (Tier S/A foundation) ──
    # These initialize even on single-server LAN deployments — they
    # degrade to in-process fallbacks when no Redis URL is configured.
    # In production (HELEN_ENV=production) the socket/server.py guard
    # has already enforced HELEN_REDIS_URL, so redis_client here will
    # always be a real connection.
    _redis_client = None
    _redis_url = (settings.HELEN_REDIS_URL or "").strip() if hasattr(settings, "HELEN_REDIS_URL") else ""
    if not _redis_url:
        import os as _os_helen
        _redis_url = _os_helen.environ.get("HELEN_REDIS_URL", "").strip()
    if _redis_url:
        try:
            import redis.asyncio as _aioredis
            _redis_client = _aioredis.from_url(_redis_url, decode_responses=False)
            await _redis_client.ping()
            logger.info("redis_client_connected", url_prefix=_redis_url.split("@")[0][:32])
        except Exception as _re:
            logger.warning("redis_client_unavailable", error=str(_re),
                           note="distributed services will degrade to in-process")
            _redis_client = None

    try:
        from app.services.discovery_service import get_server_id as _get_sid
        _this_sid = _get_sid()
    except Exception:
        _this_sid = "local"

    from app.services import distributed_lock_service as _dls
    _dls.configure(redis_client=_redis_client)

    from app.services import distributed_presence_service as _dps
    _dps.configure(redis_client=_redis_client, this_server_id=_this_sid)

    from app.services import server_registry_service as _srs
    _registry = _srs.configure(
        redis_client=_redis_client,
        this_server_id=_this_sid,
        region=getattr(settings, "HELEN_REGION", "default"),
        version=getattr(settings, "VERSION", "unknown"),
        capacity_max_calls=getattr(settings, "MAX_ACTIVE_CALLS", 500),
        capacity_max_users=getattr(settings, "MAX_ACTIVE_USERS", 5000),
        sfu_available=getattr(settings, "SFU_ENABLED", False),
    )
    await _registry.register()
    await _registry.heartbeat_loop_start()

    from app.services import origin_election_service as _oes
    _election = _oes.configure(
        lock_service=_dls.get_lock_service(),
        registry_service=_registry,
        this_server_id=_this_sid,
    )
    await _election.sweeper_loop_start()

    from app.services import event_priority_queue as _epq
    _epq.configure()

    # Distributed group call state — Redis-backed mirror of group
    # participant data so any server can read participants without
    # round-tripping the origin. Falls back to in-process when no
    # redis_client.
    from app.services import distributed_group_call_state as _dgcs
    _dgcs.configure(
        redis_client=_redis_client,
        this_server_id=_this_sid,
    )

    # Broker client (Redis Streams adapter, with in-process fallback)
    from app.services import broker_client as _bc
    _broker = await _bc.configure(
        redis_client=_redis_client,
        this_server_id=_this_sid,
    )

    # Event ACK manager — wired with DLQ recorder so timeouts land
    # in the existing dead_letter_service for inspection / replay.
    from app.services import event_ack_manager as _eam
    async def _ack_dlq_recorder(env, reason: str):
        try:
            from app.services import dead_letter_service as _dls_mod
            await _dls_mod.record(
                kind="federation_emit",
                reason=reason,
                payload=env.model_dump(),
            )
        except Exception:
            pass
    _ack_mgr = _eam.configure(dlq_recorder=_ack_dlq_recorder)

    # Route planner & executor.
    from app.services import route_planner as _rp
    _planner = _rp.configure(
        registry_service=_registry,
        this_server_id=_this_sid,
    )

    from app.services import route_executor as _rex
    async def _exec_local_deliver(env):
        # Local delivery hook: emit the envelope's payload via
        # Socket.IO to the destination user. The envelope's
        # event_type IS the socket event name in our convention.
        if not env.destination_user_id:
            return False
        try:
            from app.socket.server import emit_to_user as _eu
            await _eu(env.event_type, env.payload, env.destination_user_id)
            # Trace the delivery hop.
            try:
                from app.services.trace_collector_service import trace_collector
                await trace_collector.record_hop(
                    env, action="delivered",
                )
            except Exception:
                pass
            return True
        except Exception as _le:
            logger.warning("route_executor_local_deliver_failed",
                           event_id=env.event_id, error=str(_le))
            return False
    async def _exec_dlq_recorder(env, reason: str):
        try:
            from app.services import dead_letter_service as _dls_mod
            await _dls_mod.record(
                kind="federation_emit",
                reason=f"executor:{reason}",
                payload=env.model_dump(),
            )
            from app.services.trace_collector_service import trace_collector
            await trace_collector.record_hop(env, action="dlq", notes=reason)
        except Exception:
            pass
    _executor = _rex.configure(
        this_server_id=_this_sid,
        presence_service=_dps.get_presence_service(),
        registry_service=_registry,
        route_planner=_planner,
        broker_client=_broker,
        ack_manager=_ack_mgr,
        local_deliver_fn=_exec_local_deliver,
        dlq_recorder=_exec_dlq_recorder,
    )

    # Fabric subscribers — receive side of the broker. Background
    # tasks per priority pull envelopes scoped to this server and
    # dispatch via _exec_local_deliver.
    from app.socket import server_fabric_handlers as _sfh
    _fabric = _sfh.configure(
        this_server_id=_this_sid,
        broker_client=_broker,
        ack_manager=_ack_mgr,
        local_deliver_fn=_exec_local_deliver,
        priority_router=_epq.get_router(),
    )
    await _fabric.start()

    # Trace collector reaper — purges traces older than retention.
    try:
        from app.services.trace_collector_service import trace_collector
        await trace_collector.start_reaper_loop()
    except Exception as _te:
        logger.warning("trace_collector_start_failed", error=str(_te))

    # Peer eviction sweeper — drops stale WAITING/PENDING peers so
    # the admin queue doesn't fill up with abandoned discoveries.
    try:
        from app.services.peer_acceptance_policy import get_policy as _get_policy
        _get_policy().validate_mode_config()
        async def _peer_eviction_loop():
            import asyncio as _a
            from app.services.peer_approval_service import peer_approval_service
            # Sweep every 10 minutes — TTL is 24h by default so this
            # is plenty fine-grained.
            while True:
                try:
                    await _a.sleep(600)
                    await peer_approval_service.evict_stale_waiting()
                except _a.CancelledError:
                    return
                except Exception as _e:
                    logger.warning("peer_eviction_loop_iter_failed", error=str(_e))
        import asyncio as _peer_evict_asyncio
        _peer_eviction_task = _peer_evict_asyncio.create_task(_peer_eviction_loop())
    except Exception as _pe:
        logger.warning("peer_eviction_start_failed", error=str(_pe))
        _peer_eviction_task = None

    from app.services import load_monitor as _lm
    # Wire optional providers from existing services if available.
    def _socket_count_provider():
        try:
            from app.socket.server import sio as _sio
            return len(getattr(_sio.manager, "rooms", {}).get("/", {}))
        except Exception:
            return 0
    def _calls_count_provider():
        try:
            from app.services.call_service import call_service
            return len(call_service._active_calls) if hasattr(call_service, "_active_calls") else 0
        except Exception:
            return 0
    _monitor = _lm.configure(
        this_server_id=_this_sid,
        registry_service=_registry,
        priority_router=_epq.get_router(),
        socket_count_provider=_socket_count_provider,
        active_calls_provider=_calls_count_provider,
    )
    await _monitor.start()
    logger.info("distributed_services_started",
                redis=_redis_client is not None,
                server_id=_this_sid)

    # Manual peer seeding — used when UDP broadcast isn't available
    # (different broadcast ports per instance during chain-routing
    # tests, or a LAN segment that filters multicast). Reads
    # HELEN_SEED_PEERS="host1:port1,host2:port2" and probes each.
    try:
        from app.services.peer_registry import seed_peers_from_env
        seeded = await seed_peers_from_env()
        if seeded:
            logger.info("peer_seeded_from_env_total", count=seeded)
    except Exception as _e:
        logger.warning("peer_seed_failed", error=str(_e))

    # ── DHT bootstrap (opt-in, large-cluster support) ──────
    # Restore a previously-persisted routing table only if the operator
    # explicitly enabled persistence. Cold-start works fine without it
    # — peers populate the routing table naturally as UDP discovery,
    # gossip, and on-demand DHT lookups encounter them.
    # Restore in-flight sagas from disk so a crash mid-saga doesn't
    # silently drop work — the operator sees previously-running sagas
    # in the admin panel and can decide to resume or compensate them.
    # Auto-resume is opt-in (HELEN_SAGA_AUTO_RESUME=1) since some
    # forward steps are non-idempotent.
    try:
        from app.services.saga_engine import get_saga_engine
        loaded = get_saga_engine().load_from_disk()
        if loaded:
            logger.info("saga_state_restored", count=loaded)
            import os as _os_saga
            if _os_saga.environ.get("HELEN_SAGA_AUTO_RESUME", "").lower() in {"1", "true", "yes", "on"}:
                resumed = await get_saga_engine().resume_pending()
                logger.info("saga_auto_resumed", count=resumed)
    except Exception as e:
        logger.warning("saga_recovery_failed", error=str(e))

    try:
        import os as _os_dht
        if _os_dht.environ.get("HELEN_DHT_PERSIST", "").lower() in {"1", "true", "yes", "on"}:
            from app.services.dht_kademlia import load_routing_table_from_disk
            loaded = load_routing_table_from_disk()
            if loaded:
                logger.info("kademlia_state_restored", count=loaded)
        boot = (_os_dht.environ.get("HELEN_BOOTSTRAP_NODES") or "").strip()
        if boot:
            from app.services.peer_registry import peer_registry
            from app.services.discovery_service import get_server_id
            from app.services.federation_service import federation_service
            import httpx as _httpx_b
            async with _httpx_b.AsyncClient(timeout=4.0) as _client:
                for entry in boot.split(","):
                    entry = entry.strip()
                    if not entry or ":" not in entry:
                        continue
                    host, _, port_s = entry.partition(":")
                    if not port_s.isdigit():
                        continue
                    try:
                        r = await _client.get(f"http://{host}:{port_s}/api/discovery")
                        if r.status_code != 200:
                            continue
                        await peer_registry.ingest(r.json(), from_ip=host)
                    except Exception as _be:
                        logger.warning("bootstrap_probe_failed",
                                       entry=entry, error=str(_be))
            # Now ask each known bootstrap peer for its closest neighbors
            # to our own server_id — populates the routing table.
            try:
                my_id = get_server_id()
                for p in await peer_registry.list(include_stale=False):
                    try:
                        body = {"target_id": my_id, "k": 20}
                        # Re-use signed_request via the public client.
                        await federation_service._signed_request(
                            p, "POST",
                            "/api/federation/dht/find_node",
                            json_body=body,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as _e:
        logger.warning("dht_bootstrap_failed", error=str(_e))

    # Periodic snapshot of the routing table to disk so a restarted
    # server keeps its DHT view rather than re-walking the network.
    # OPT-IN: only useful for very-large clusters that take measurable
    # time to populate the routing table. Default off keeps the I/O
    # off the hot path.
    import os as _os_pers
    if _os_pers.environ.get("HELEN_DHT_PERSIST", "").lower() in {"1", "true", "yes", "on"}:
        async def _kademlia_persist_loop():
            import asyncio as _aio
            from app.services.dht_kademlia import save_routing_table_to_disk
            while True:
                await _aio.sleep(300.0)  # every 5 minutes
                try:
                    save_routing_table_to_disk()
                except Exception as _e:
                    logger.debug("kademlia_persist_failed", error=str(_e))
        import asyncio as _asyncio_dht
        _asyncio_dht.create_task(_kademlia_persist_loop())

    # Periodic STORE re-announce — turned OFF by default. Bridges
    # form on-demand instead: when server B's user emits to a target
    # hosted on server A, the iterative lookup walks the DHT (with K=20
    # closest forwarding as fallback), and ``route_learned_hint``
    # caches the resolved origin for 15 minutes. No proactive announce
    # = zero overhead when there's no cross-server traffic.
    #
    # Operators running heavy multi-server chat workloads can flip the
    # ``HELEN_DHT_ACTIVE_ANNOUNCE`` env to make the very first emit
    # for a user resolve in O(log N) instead of falling back to flood.
    if _os_pers.environ.get("HELEN_DHT_ACTIVE_ANNOUNCE", "").lower() in {"1", "true", "yes", "on"}:
        async def _kademlia_announce_loop():
            import asyncio as _aio
            from app.services.presence_service import presence_service
            from app.services.dht_lookup import announce_user_to_dht
            while True:
                await _aio.sleep(60.0)
                try:
                    online_uids = await presence_service.get_online_user_ids()
                except Exception:
                    continue
                sem = _aio.Semaphore(16)

                async def _go(uid):
                    async with sem:
                        try:
                            await announce_user_to_dht(uid, ttl_seconds=120.0)
                        except Exception:
                            pass

                await _aio.gather(*(_go(u) for u in online_uids))
        import asyncio as _asyncio_dht_a
        _asyncio_dht_a.create_task(_kademlia_announce_loop())
    # SSDP responder — answers M-SEARCH on 239.255.255.250:1900. Optional:
    # the bind can fail if Windows' SSDP Discovery service owns port 1900,
    # in which case we log and carry on (mDNS + broadcast still cover us).
    try:
        from app.services.ssdp_responder import ssdp_responder
        await ssdp_responder.start()
    except Exception as _e:
        logger.warning("ssdp_responder_start_failed", error=str(_e))
    # mDNS discovery — parallel channel to UDP broadcast, mainly for
    # Bonjour-aware LANs (macOS/iOS) and managed switches that drop
    # directed UDP broadcasts. Failure is non-fatal.
    try:
        from app.services.mdns_discovery import start_mdns_discovery
        from app.services.discovery_service import get_server_id as _gsid
        import os as _os_mdns
        start_mdns_discovery(
            my_server_id=_gsid() or "anon",
            port=int(_os_mdns.environ.get("PORT", 3000)),
            cluster_id=settings.COMMCLIENT_CLUSTER_ID,
            version="1.0.0",
            bridge=False,
        )
    except Exception as _e:
        logger.warning("mdns_init_failed", error=str(_e))

    # Periodic state reconciliation between peers (trust DB +
    # sync_policy convergence over last-write-wins).
    try:
        from app.services.state_reconciliation import start_reconciliation_loop
        start_reconciliation_loop()
    except Exception as _e:
        logger.warning("reconciliation_init_failed", error=str(_e))

    # Active latency probing — keeps path_health fresh even when the
    # relay chain is idle, so routing decisions are based on current
    # link quality rather than stale samples.
    try:
        from app.services.latency_prober import start_latency_prober
        start_latency_prober()
    except Exception as _e:
        logger.warning("latency_prober_init_failed", error=str(_e))

    # Anti-entropy gossip — Merkle-style continuous convergence of
    # the trust DB; complements the slower reconciliation loop.
    try:
        from app.services.anti_entropy import start_anti_entropy_loop
        start_anti_entropy_loop()
    except Exception as _e:
        logger.warning("anti_entropy_init_failed", error=str(_e))

    # Network partition / split-brain detection — emits events on
    # quorum changes and triggers aggressive convergence on heal.
    try:
        from app.services.partition_detector import start_partition_detector
        start_partition_detector()
    except Exception as _e:
        logger.warning("partition_detector_init_failed", error=str(_e))

    # Cluster-consensus time — bounds HMAC clock-skew failures even
    # when the host's NTP isn't reachable.
    try:
        from app.services.cluster_time import start_cluster_time_sync
        start_cluster_time_sync()
    except Exception as _e:
        logger.warning("cluster_time_init_failed", error=str(_e))

    # Bandwidth probing — feeds throughput into load balancer.
    try:
        from app.services.bandwidth_probe import start_bandwidth_probe
        start_bandwidth_probe()
    except Exception as _e:
        logger.warning("bandwidth_probe_init_failed", error=str(_e))

    # Backpressure controller — overload gate for upstream peers.
    try:
        from app.services.backpressure import start_backpressure_loop
        start_backpressure_loop()
    except Exception as _e:
        logger.warning("backpressure_init_failed", error=str(_e))

    # Replication heal loop — keeps K replicas of critical records.
    try:
        from app.services.replication_manager import start_replication_heal
        start_replication_heal()
    except Exception as _e:
        logger.warning("replication_heal_init_failed", error=str(_e))

    # Multi-path router — auto-mode adaptive routing across all
    # available paths; selects route class based on live conditions.
    try:
        from app.services.multipath_router import start_multipath_router
        start_multipath_router()
    except Exception as _e:
        logger.warning("multipath_router_init_failed", error=str(_e))

    # Daily audit-chain compaction — gated by a cluster-wide lock so
    # only one peer compacts at a time.
    try:
        from app.services.log_compaction import start_log_compaction
        start_log_compaction()
    except Exception as _e:
        logger.warning("log_compaction_init_failed", error=str(_e))

    # Topology manager — pulls live state from services into the
    # graph package every 30s and persists to data/topology.json.
    try:
        from app.topology import start_topology_manager
        start_topology_manager()
    except Exception as _e:
        logger.warning("topology_manager_init_failed", error=str(_e))

    # Routing strategy manager — pluggable composable router that
    # orchestrates 9 strategies into a single RouteDecision.
    try:
        from app.routing_strategy import start_strategy_manager
        start_strategy_manager()
    except Exception as _e:
        logger.warning("routing_strategy_init_failed", error=str(_e))

    # Distributed-system orchestrator — composes membership /
    # heartbeat / recovery into a single managed lifecycle.
    try:
        from app.distributed_system import start_distributed_system
        start_distributed_system()
    except Exception as _e:
        logger.warning("distributed_system_init_failed", error=str(_e))

    # Monitoring stack — health checks + metrics + alerts + topology
    # snapshots + dashboard renderer. All loops independent.
    try:
        from app.monitoring import start_monitoring
        start_monitoring()
    except Exception as _e:
        logger.warning("monitoring_init_failed", error=str(_e))

    # P2P layer — peer registry / discovery / gossip / DHT / selection
    # / NAT traversal / sessions, all behind a single manager.
    try:
        from app.p2p import start_p2p
        start_p2p()
    except Exception as _e:
        logger.warning("p2p_init_failed", error=str(_e))

    # Overlay networks — logical graphs (ring/tree/topic) layered
    # over the physical mesh. Persisted to data/overlay_state.json.
    try:
        from app.overlay import start_overlay
        start_overlay()
    except Exception as _e:
        logger.warning("overlay_init_failed", error=str(_e))

    # Resilient networking — circuit breakers + retry queue +
    # recovery + degraded-mode flag. Persistent retry queue at
    # data/resilience_retry_queue.jsonl.
    try:
        from app.resilience import start_resilience
        start_resilience()
    except Exception as _e:
        logger.warning("resilience_init_failed", error=str(_e))

    # NAT traversal — STUN detection + UDP/TCP hole-punch + reverse
    # tunnel + relay-fallback ladder, gated by env config.
    try:
        from app.nat import start_nat
        start_nat()
    except Exception as _e:
        logger.warning("nat_init_failed", error=str(_e))

    # Auth refresh-token pruner — drops expired/revoked rows after
    # MAX_AGE_DAYS (default 90) under cluster-wide lock.
    try:
        from app.services.auth_token_pruner import start as start_auth_pruner
        start_auth_pruner()
    except Exception as _e:
        logger.warning("auth_token_pruner_init_failed", error=str(_e))

    # Push offline batcher — combines offline pushes into single
    # notifications instead of bombing the user when they reconnect.
    try:
        from app.services.push_offline_batcher import get_push_batcher
        get_push_batcher().start()
    except Exception as _e:
        logger.warning("push_batcher_init_failed", error=str(_e))

    # Cross-cluster gossip — exchange peer summaries + blocklist
    # with foreign clusters listed in HELEN_FEDERATED_CLUSTERS.
    try:
        from app.services.cross_cluster_gossip import start as start_cc_gossip
        start_cc_gossip()
    except Exception as _e:
        logger.warning("cross_cluster_gossip_init_failed", error=str(_e))

    # Anomaly detector — z-score over rolling metric windows.
    try:
        from app.services.anomaly_detector import get_anomaly_detector
        get_anomaly_detector().start()
    except Exception as _e:
        logger.warning("anomaly_detector_init_failed", error=str(_e))

    # HTTP connection-pool reaper — closes idle keep-alive clients
    # after IDLE_TIMEOUT_SEC seconds of disuse.
    try:
        from app.services.http_connection_pool import get_pool
        get_pool().start()
    except Exception as _e:
        logger.warning("http_pool_init_failed", error=str(_e))

    # Metrics history — SQLite persistence beyond rolling window so
    # capacity_planner can forecast farther.
    try:
        from app.services.metrics_history import get_metrics_history
        get_metrics_history().start()
    except Exception as _e:
        logger.warning("metrics_history_init_failed", error=str(_e))

    # Service Discovery — production endpoint resolution. Self-
    # registers as PEER + role-specific service types; reaper
    # evicts stale entries on TTL.
    try:
        from app.service_discovery import start_discovery
        start_discovery()
    except Exception as _e:
        logger.warning("service_discovery_init_failed", error=str(_e))

    # Plugin loader — scan HELEN_PLUGIN_DIR (default data/plugins/)
    # for *.py files exposing a HOOKS dict.
    try:
        from app.services.plugin_loader import get_plugins
        get_plugins().load_all()
    except Exception as _e:
        logger.warning("plugin_loader_init_failed", error=str(_e))

    logger.info("discovery_services_started")

    # Start federation UDP relay manager (multi-hop call transit)
    from app.services.relay_worker import relay_manager
    try:
        await relay_manager.start(bind_host="0.0.0.0")
    except Exception as _e:
        logger.warning("relay_manager_start_failed", error=str(_e))

    # Start federated presence resync loop (cross-server directory)
    if settings.FEDERATION_ENABLED and settings.FEDERATION_SECRET:
        try:
            from app.services.federated_presence import federated_presence
            await federated_presence.start_resync_loop()
            # Also subscribe to distributed_presence_service Redis
            # pub/sub so offline events drop the cached entry within
            # 1s instead of waiting up to 120s for TTL expiry.
            await federated_presence.start_distributed_listener()
            logger.info("federated_presence_started")
        except Exception as _e:
            logger.warning("federated_presence_start_failed", error=str(_e))

        # Gossip worker — periodically pushes our peer list to a random
        # subset of known peers so discovery transcends UDP broadcast
        # range. Bounded fanout (sqrt(N)+2) keeps network load O(N·√N)
        # instead of O(N²) as the mesh grows past a few hundred nodes.
        async def _gossip_loop():
            import math as _math
            import random as _random
            from app.services.peer_registry import peer_registry
            from app.services.federation_service import federation_service
            from app.services import federation_metrics as _metrics
            import asyncio as _asyncio_g
            while True:
                try:
                    await _asyncio_g.sleep(20.0)
                    peers = await peer_registry.list(include_stale=False)
                    if len(peers) < 2:
                        continue
                    fanout = min(len(peers), int(_math.sqrt(len(peers))) + 2)
                    targets = _random.sample(peers, fanout)
                    for peer in targets:
                        try:
                            n = await federation_service.gossip_peers_to(peer, peers)
                            _metrics.bump_peer(peer.server_id, emits_sent=1)
                            if n > 0:
                                _metrics.record_event(
                                    "gossip_sent",
                                    peer=peer.server_id,
                                    offered=len(peers) - 1,
                                    ingested=n,
                                )
                        except Exception as _e2:
                            logger.debug("gossip_send_failed",
                                         peer=peer.server_id, error=str(_e2))
                except Exception as _e3:
                    logger.warning("gossip_loop_error", error=str(_e3))

        import asyncio as _asyncio_for_gossip
        _gossip_task = _asyncio_for_gossip.create_task(_gossip_loop())
        logger.info("federation_gossip_started")

    from app.services.discovery_service import get_lan_ip
    logger.info("server_ready", lan_ip=get_lan_ip(), port=settings.PORT)

    # Connectivity orchestrator — boots reverse tunnel / relay / etc. if
    # their env vars are present. Failures are logged but never raised so a
    # misconfigured tunnel can't block the server from starting.
    try:
        from app.services.connectivity import orchestrator as _conn
        await _conn.start()
        logger.info("connectivity_orchestrator_started")
    except Exception as _e:
        logger.warning("connectivity_orchestrator_start_failed", error=str(_e))

    # Start call orphan cleanup loop
    from app.services.call_service import call_service
    call_service.start_cleanup_loop()
    logger.info("call_cleanup_loop_started")

    # Start batched participant writer — coalesces mass-join SQLite writes.
    from app.services.call_participant_batcher import call_participant_batcher
    call_participant_batcher.start()
    logger.info("call_participant_batcher_started")

    # Sweep orphan active_calls rows BEFORE rehydrating — a previous
    # crash may have left rows with status='active' and stale heartbeats.
    # If we rehydrate those, they'd appear in-memory as live calls
    # nobody can join (the participants have all reconnected fresh).
    # The periodic sweep loop catches them eventually but doing it
    # synchronously at startup avoids the noisy "ghost call" window.
    try:
        from app.services.call_state_persistence import call_state_persistence as _csp_startup
        startup_orphans = await _csp_startup.sweep_orphans()
        if startup_orphans:
            logger.info("startup_orphan_calls_swept", count=len(startup_orphans))
    except Exception as _e:
        logger.warning("startup_orphan_sweep_failed", error=str(_e))

    # Rehydrate active calls from DB (crash / restart / deploy recovery)
    try:
        restored = await call_service.rehydrate_from_db()
        logger.info("call_rehydrate_done", restored=restored)
    except Exception as _e:
        logger.error("call_rehydrate_startup_failed", error=str(_e))

    # Periodically sweep orphan calls (multi-worker safe; heartbeat timeout)
    import asyncio as _asyncio_mod

    # ── Leader election wiring ──────────────────────────
    # All singleton-sensitive loops below are gated behind
    # `run_as_leader` / `run_supervised_as_leader` so that horizontally
    # scaled workers don't fire them N times. When the backend is
    # "single" (default for SQLite/dev) every worker is leader — zero
    # overhead. When it's "postgres" or "redis", only one worker at a
    # time runs each loop.
    from app.services.leader_election import (
        LeaderLoopConfig,
        run_as_leader,
        run_supervised_as_leader,
    )
    _lease_ttl = settings.LEADER_LEASE_TTL_SECONDS

    async def _call_orphan_sweep_tick():
        from app.services.call_state_persistence import call_state_persistence as _csp
        from app.services.call_service import call_service as _cs
        swept_ids = await _csp.sweep_orphans()
        # Keep in-memory ActiveCall state + SFU routers in sync with
        # the DB's authoritative "ended" decision. Without this, stale
        # in-memory calls would keep mediasoup routers allocated and
        # allow ghost signaling traffic after a heartbeat timeout.
        if swept_ids:
            try:
                await _cs.reap_ended_calls(swept_ids)
            except Exception as _e:
                logger.error("call_reap_after_sweep_failed", error=str(_e))

    _call_sweep_task = _asyncio_mod.create_task(
        run_as_leader(LeaderLoopConfig(
            name="call_orphan_sweeper",
            interval=45.0,
            fn=_call_orphan_sweep_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=5.0,
        ))
    )
    logger.info("call_orphan_sweep_started")

    # Periodic WAL checkpoint (keeps the WAL file bounded under heavy write load)
    async def _wal_checkpoint_tick():
        from app.db.sqlite_tuning import checkpoint_wal as _ck
        try:
            await _ck(engine, mode="PASSIVE")
        except Exception as _e:
            logger.debug("wal_checkpoint_loop_error", error=str(_e))

    if settings.DB_BACKEND == "sqlite":
        # WAL is file-local to this process, but multi-worker SQLite
        # deployments are not supported anyway; leader-gate it to keep
        # the pattern uniform.
        _wal_task = _asyncio_mod.create_task(
            run_as_leader(LeaderLoopConfig(
                name="wal_checkpoint",
                interval=600.0,
                fn=_wal_checkpoint_tick,
                ttl_seconds=_lease_ttl,
                initial_delay=60.0,
            ))
        )
        logger.info("wal_checkpoint_loop_started")

    # Resumable upload GC — clears expired staging dirs
    async def _upload_gc_tick():
        from app.services.resumable_upload_service import resumable_upload_service as _rus
        await _rus.gc_expired_sessions()

    _upload_gc_task = _asyncio_mod.create_task(
        run_as_leader(LeaderLoopConfig(
            name="upload_gc",
            interval=300.0,
            fn=_upload_gc_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=30.0,
        ))
    )
    logger.info("upload_gc_loop_started")

    # Start heartbeat stale detection
    # NOTE: This loop is INTENTIONALLY NOT leader-gated. Presence is kept
    # per-worker (sid → uid map lives in each process), and
    # ``cleanup_stale_heartbeats`` only disconnects sids known to THIS
    # worker. Gating it would strand stale sockets on non-leader
    # workers. The DB-side presence record is last-write-wins and safe
    # to touch from every worker.
    import asyncio
    from app.services.presence_service import presence_service
    from app.socket.server import sio

    async def _heartbeat_cleanup_loop():
        """Periodically clean up users who stopped sending heartbeats.

        The 60s timeout used to race with legitimate long-idle sockets under
        megascale bursts — a sender socket that connected, issued a join, and
        then sat idle for ~90s while the rest of a 5k burst joined was getting
        culled before it could send a chat message. engineio's own ping/pong
        (ping_timeout=90s) is the authoritative liveness check; this loop is a
        belt-and-braces sweep for stranded app-level state and should NOT
        second-guess live sockets. 600s gives us plenty of headroom.
        """
        while True:
            try:
                await asyncio.sleep(60)
                stale_users = await presence_service.cleanup_stale_heartbeats(timeout_seconds=600)
                if stale_users:
                    for uid in stale_users:
                        sids = presence_service.get_sids(uid)
                        for sid in sids:
                            try:
                                await sio.disconnect(sid)
                            except Exception:
                                pass
                    logger.info("heartbeat_cleanup", stale_count=len(stale_users))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat_cleanup_error", error=str(e))

    _heartbeat_task = asyncio.create_task(_heartbeat_cleanup_loop())
    logger.info("heartbeat_cleanup_started")

    # ── Room cleanup ──────────────────────────────────────────
    # Drops empty channels older than 30 days + ghost users (last_seen
    # > 30d, status stuck "online"). Runs hourly; cheap because the
    # query is indexed on `updated_at`. Without this, churn-heavy
    # deployments (many short-lived rooms) accumulate dead rows
    # forever — the audit flagged the lack of cleanup as an
    # operational risk.
    async def _room_cleanup_loop():
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import delete as _sa_delete, select as _sa_select, update as _sa_update
        from app.db.session import async_session_factory as _sf
        from app.models.channel import Channel as _Ch, ChannelMember as _CM
        from app.models.user import User as _User

        while True:
            try:
                await asyncio.sleep(3600)  # hourly
                cutoff = datetime.now(timezone.utc) - timedelta(days=30)

                async with _sf() as db:
                    # Empty channels older than cutoff: anything with
                    # zero ChannelMember rows AND updated_at < cutoff.
                    stale_q = await db.execute(
                        _sa_select(_Ch.id).where(
                            _Ch.updated_at < cutoff,
                            ~_sa_select(_CM.user_id)
                                .where(_CM.channel_id == _Ch.id)
                                .exists(),
                        )
                    )
                    stale_ids = [r[0] for r in stale_q.all()]
                    if stale_ids:
                        await db.execute(
                            _sa_delete(_Ch).where(_Ch.id.in_(stale_ids))
                        )
                        logger.info("room_cleanup_dropped_empty", count=len(stale_ids))

                    # Ghost users: status='online' but last_seen too old.
                    # Reset to 'offline' so the presence roster is
                    # eventually consistent even if a disconnect was
                    # missed.
                    ghost_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
                    res = await db.execute(
                        _sa_update(_User)
                          .where(_User.status == "online", _User.last_seen < ghost_cutoff)
                          .values(status="offline")
                    )
                    if res.rowcount:
                        logger.info("room_cleanup_ghost_users_reset", count=res.rowcount)
                    await db.commit()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("room_cleanup_error", error=str(e))

    _room_cleanup_task = asyncio.create_task(_room_cleanup_loop())
    logger.info("room_cleanup_started")

    # Start audit log DB writer
    from app.core.audit import start_audit_writer
    await start_audit_writer()
    logger.info("audit_writer_started")

    # Start automated backup scheduler (no-op if AUTO_BACKUP_ENABLED=false)
    try:
        from app.services import backup_scheduler
        await backup_scheduler.start()
    except Exception as e:
        logger.warning("auto_backup_start_failed", error=str(e))

    # Start status-message expiry sweeper (clears expired custom statuses)
    async def _status_message_expiry_tick():
        from app.db.session import async_session_factory as _sf
        from app.services.user_service import UserService as _US
        async with _sf() as db:
            cleared = await _US.expire_status_messages(db)
        if cleared:
            logger.info("status_messages_expired", count=cleared)

    _status_msg_task = asyncio.create_task(
        run_as_leader(LeaderLoopConfig(
            name="status_message_expiry",
            interval=60.0,
            fn=_status_message_expiry_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=10.0,
        ))
    )
    logger.info("status_message_expiry_started")

    # Start channel-mute expiry sweeper (auto-unmute when mute_until passes)
    async def _channel_mute_expiry_tick():
        from app.db.session import async_session_factory as _sf
        from app.services.channel_service import ChannelService as _CS
        async with _sf() as db:
            cleared = await _CS.expire_mutes(db)
        if cleared:
            logger.info("channel_mutes_expired", count=cleared)

    _mute_task = asyncio.create_task(
        run_as_leader(LeaderLoopConfig(
            name="channel_mute_expiry",
            interval=60.0,
            fn=_channel_mute_expiry_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=15.0,
        ))
    )
    logger.info("channel_mute_expiry_started")

    # Start scheduled message dispatch worker
    # CRITICAL: MUST be leader-gated — running on N workers causes
    # every scheduled message to be delivered N times before the
    # `_claim_due` optimistic update serializes them.
    from app.services.scheduled_message_service import ScheduledMessageService
    _scheduled_task = asyncio.create_task(
        run_supervised_as_leader(
            "scheduled_message_dispatcher",
            lambda: ScheduledMessageService.run_dispatch_loop(),
            ttl_seconds=_lease_ttl,
            initial_delay=5.0,
        )
    )
    logger.info("scheduled_message_worker_task_created")

    # Start webhook outbound dispatcher
    # CRITICAL: leader-only — duplicates produce webhook retries and
    # pollute downstream audit trails.
    from app.services.webhook_service import WebhookService
    _webhook_stop = asyncio.Event()
    _webhook_task = asyncio.create_task(
        run_supervised_as_leader(
            "webhook_dispatcher",
            lambda: WebhookService.run_dispatch_loop(_webhook_stop),
            ttl_seconds=_lease_ttl,
            initial_delay=5.0,
        )
    )
    logger.info("webhook_dispatch_loop_started")

    # Start poll auto-close sweeper
    async def _poll_expiry_tick():
        from app.db.session import async_session_factory as _sf
        from app.services.poll_service import PollService as _PS
        async with _sf() as db:
            closed = await _PS.expire_due(db)
        if closed:
            logger.info("polls_auto_closed", count=closed)

    _poll_task = asyncio.create_task(
        run_as_leader(LeaderLoopConfig(
            name="poll_expiry",
            interval=60.0,
            fn=_poll_expiry_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=20.0,
        ))
    )
    logger.info("poll_expiry_started")

    # Start DLQ reaper — periodically retries failed side-effects that
    # were persisted via `app.services.dead_letter_service.record()`.
    # Leader-gated: duplicate reapers would replay the same failed
    # fan-out from multiple workers, double-delivering messages.
    from app.services.dead_letter_service import DeadLetterService

    async def _dlq_reaper_supervised():
        # DeadLetterService.start()/stop() is designed for a single
        # owner. Wrap it as a coroutine the supervisor can cancel.
        await DeadLetterService.start()
        try:
            # DeadLetterService._reaper_task is the actual loop — we
            # just await its completion here; supervisor cancels us
            # on lease loss, which propagates to `stop()`.
            if DeadLetterService._reaper_task is not None:
                await DeadLetterService._reaper_task
        finally:
            try:
                await DeadLetterService.stop()
            except Exception:
                pass

    _dlq_task = asyncio.create_task(
        run_supervised_as_leader(
            "dlq_reaper",
            lambda: _dlq_reaper_supervised(),
            ttl_seconds=_lease_ttl,
            initial_delay=10.0,
        )
    )
    logger.info("dlq_reaper_task_created")

    # Start group-file-offer sweeper — expires stale multicast offers
    # and abandons recipients that stopped making progress so the
    # channel dashboards stay accurate.
    async def _group_file_sweep_tick():
        from app.db.session import async_session_factory as _sf
        from app.services.group_file_service import GroupFileService as _GFS
        async with _sf() as db:
            expired = await _GFS.sweep_expired(db)
        async with _sf() as db:
            stale = await _GFS.cleanup_stale_recipients(db)
        if expired or stale:
            logger.info(
                "group_file_sweep_done",
                expired=expired, stale=stale,
            )

    _group_file_task = asyncio.create_task(
        run_as_leader(LeaderLoopConfig(
            name="group_file_sweeper",
            interval=120.0,
            fn=_group_file_sweep_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=30.0,
        ))
    )
    logger.info("group_file_sweep_started")

    # Daily SQLite snapshot — fires every 24 h, rotates to 7 copies. Leader
    # gated so horizontally scaled workers don't fan out duplicate writes.
    # Runs off by default on the first tick too (initial_delay=3600) so a
    # fresh install doesn't snapshot an empty DB as its first artefact.
    async def _daily_backup_tick():
        from app.services.backup_service import backup_service as _bs
        try:
            name = await _bs.create_backup()
            deleted = await _bs.auto_cleanup(keep_count=7)
            logger.info("daily_backup_created", name=name, rotated_out=deleted)
        except Exception as _e:
            logger.error("daily_backup_failed", error=str(_e))

    _backup_task = asyncio.create_task(
        run_as_leader(LeaderLoopConfig(
            name="daily_backup",
            interval=86400.0,           # 24 h
            fn=_daily_backup_tick,
            ttl_seconds=_lease_ttl,
            initial_delay=3600.0,       # 1 h post-boot
        ))
    )
    logger.info("daily_backup_started")

    # ── Control plane: automatic decision engine ──────────
    try:
        from app.services.control_plane import ControlPlane
        await ControlPlane.instance().start()
    except Exception as _e:
        logger.warning("control_plane_start_failed", error=str(_e))

    # ── Cluster mesh: auto-discover peers + transitive discovery ──
    try:
        from app.services.cluster_mesh import get_mesh
        await get_mesh().start()
    except Exception as _e:
        logger.warning("cluster_mesh_start_failed", error=str(_e))

    # ── Secret admin master code (generated once, printed once) ──
    try:
        from app.api.routes.secret_admin import ensure_master_code
        ensure_master_code()
    except Exception as _e:
        logger.warning("secret_master_code_ensure_failed", error=str(_e))

    # ── Helen-Vault master code (separate realm, LAN-only) ──
    try:
        from app.api.routes.vault import ensure_master_code as _vault_ensure
        _vault_ensure()
    except Exception as _e:
        logger.warning("vault_master_code_ensure_failed", error=str(_e))

    # Install signal handlers so a SIGTERM (docker stop / systemctl
    # stop / Ctrl-C) gives the lifespan a chance to drain DLQ +
    # checkpoint sagas + notify clients before the process is killed.
    # uvicorn already handles SIGINT but we add SIGTERM so Linux
    # service managers get the same graceful path.
    import signal as _sig, asyncio as _asyncio_sig
    _shutdown_event = _asyncio_sig.Event()

    def _sig_handler(signame: str) -> None:
        if not _shutdown_event.is_set():
            logger.info("signal_received_starting_graceful_shutdown",
                        signal=signame)
            _shutdown_event.set()

    try:
        _loop = _asyncio_sig.get_running_loop()
        for _sname in ("SIGTERM", "SIGINT"):
            try:
                _loop.add_signal_handler(
                    getattr(_sig, _sname),
                    _sig_handler, _sname,
                )
            except (NotImplementedError, RuntimeError):
                # Windows / threads-without-loop — uvicorn will handle.
                pass
    except Exception as _e:
        logger.debug("signal_handler_install_failed", error=str(_e))

    # ── Call topology orchestrators ───────────────────────
    # Both orchestrators (mesh↔SFU, and the 7-tier large-call ladder)
    # need a broadcaster that emits ``call:topology_change`` to every
    # participant of the call. We bind them to Socket.IO's room emit
    # — call rooms are already keyed on `call:{call_id}`.
    try:
        from app.services.sfu_orchestrator import set_broadcaster as _sfu_set_b
        from app.services.large_call_orchestrator import (
            set_large_call_broadcaster as _lco_set_b,
            get_large_call_orchestrator as _lco_get,
        )

        async def _topology_broadcast(call_id: str, event: str, payload: dict) -> None:
            from app.socket.server import sio as _sio
            await _sio.emit(event, payload, room=f"call:{call_id}")

        _sfu_set_b(_topology_broadcast)
        _lco_set_b(_topology_broadcast)

        # Wire cascading-SFU callbacks. Without these, the orchestrator
        # decides "we should be in sfu_xlarge" and then has no way to
        # actually spawn the second worker — so 200+ peer calls run on
        # one worker and saturate. With these, an additional worker is
        # allocated per ~200 peers and producers are piped between them.
        try:
            from app.services.topology_manager import (
                topology_manager as _topo_mgr,
                MediasoupBridge as _MediasoupBridge,
            )

            async def _spawn_worker(call_id: str) -> str:
                backend = _topo_mgr._backend
                if not isinstance(backend, _MediasoupBridge):
                    raise RuntimeError("mediasoup backend not configured")
                resp = await backend.spawn_worker(call_id)
                return str(resp.get("worker_id") or resp.get("router_id"))

            async def _pipe_workers(
                call_id: str, from_worker: str, to_worker: str,
            ) -> None:
                backend = _topo_mgr._backend
                if not isinstance(backend, _MediasoupBridge):
                    raise RuntimeError("mediasoup backend not configured")
                await backend.pipe_to_worker(
                    call_id, from_worker, to_worker, producer_ids=None,
                )

            _orch = _lco_get()
            _orch.spawn_sfu_worker = _spawn_worker
            _orch.pipe_workers = _pipe_workers
            logger.info("cascading_sfu_wired")
        except Exception as _ce:
            logger.warning("cascading_sfu_wire_failed", error=str(_ce))

        # Wire broadcast coalescer for high-frequency events
        # (active-speaker, participant-state, quality-report). The
        # coalescer batches latest-only payloads per key, then
        # flushes every 100ms via concurrent emits — turning a
        # 500-person × 5Hz speaker storm from 2,500 sequential
        # emits/sec into 10 batched emits/sec.
        try:
            from app.services import broadcast_coalescer as _bcc

            async def _coalescer_emit(event: str, payload: dict, room: str) -> None:
                from app.socket.server import sio as _sio_c
                await _sio_c.emit(event, payload, room=room)

            _bcc.configure(emit=_coalescer_emit, flush_interval_sec=0.1)
            _coalescer = _bcc.get_broadcast_coalescer()
            if _coalescer is not None:
                await _coalescer.start()
            logger.info("broadcast_coalescer_wired")
        except Exception as _bce:
            logger.warning("broadcast_coalescer_wire_failed", error=str(_bce))

        # Bounded per-socket egress queue — protects the server
        # from slow clients backpressuring the write pipe by capping
        # each socket's pending buffer at 256 events with priority-
        # aware drop-oldest. Without this a paused tab on a 500-
        # person call can OOM the box in minutes.
        try:
            from app.services import socket_egress_queue as _seq
            from typing import Any as _Any
            async def _seq_emit(event: str, payload: _Any, *, to: str) -> None:
                from app.socket.server import sio as _sio_q
                await _sio_q.emit(event, payload, to=to)
            _seq.configure(
                emit=_seq_emit,
                capacity=256,
                flush_interval_sec=0.05,
            )
            _eq = _seq.get_socket_egress_queue()
            if _eq is not None:
                await _eq.start()
            logger.info("socket_egress_queue_wired")
        except Exception as _eqe:
            logger.warning("socket_egress_queue_wire_failed", error=str(_eqe))

        logger.info("call_orchestrators_wired")
    except Exception as _oe:
        logger.warning("call_orchestrators_wire_failed", error=str(_oe))

    # ── LAN push manager ──────────────────────────────────
    # Singleton that fans out notifications to every live socket of
    # a user, queues missed events for offline devices (24h TTL),
    # and sends Wake-on-LAN packets where the device's MAC is known.
    try:
        from app.services.lan_push import configure_lan_push

        async def _lp_emit(sid: str, event: str, payload: dict) -> None:
            from app.socket.server import sio as _sio
            await _sio.emit(event, payload, to=sid)

        configure_lan_push(emit_to_socket=_lp_emit)
        logger.info("lan_push_manager_configured")
    except Exception as _lpe:
        logger.warning("lan_push_manager_configure_failed",
                       error=str(_lpe))

    # ── Optional alternate broker backend (NATS / MQTT) ───
    # Default = Redis Streams (already configured above). Operators
    # can swap by setting HELEN_BROKER_BACKEND. Fail-soft: if the
    # backend can't connect we keep the Redis broker running.
    _broker_backend = os.environ.get(
        "HELEN_BROKER_BACKEND", "redis",
    ).strip().lower()
    if _broker_backend == "nats":
        try:
            from app.services.nats_adapter import configure_nats
            _nats_url = os.environ.get(
                "HELEN_NATS_URL", "nats://127.0.0.1:4222",
            ).strip()
            await configure_nats(_nats_url)
            logger.info("nats_broker_configured", url_prefix=_nats_url[:32])
        except Exception as _ne:
            logger.warning("nats_broker_configure_failed",
                           error=str(_ne))
    elif _broker_backend == "mqtt":
        try:
            from app.services.mqtt_adapter import configure_mqtt
            _mqtt_host = os.environ.get(
                "HELEN_MQTT_HOST", "127.0.0.1",
            ).strip()
            _mqtt_port = int(os.environ.get("HELEN_MQTT_PORT", "1883"))
            _mqtt_user = os.environ.get("HELEN_MQTT_USERNAME") or None
            _mqtt_pass = os.environ.get("HELEN_MQTT_PASSWORD") or None
            _mqtt_tls = os.environ.get(
                "HELEN_MQTT_TLS", "",
            ).lower() in ("1", "true", "yes")
            await configure_mqtt(
                host=_mqtt_host, port=_mqtt_port,
                username=_mqtt_user, password=_mqtt_pass,
                use_tls=_mqtt_tls,
                client_id=f"helen-{_this_sid[:12]}",
            )
            logger.info("mqtt_broker_configured",
                        host=_mqtt_host, port=_mqtt_port,
                        tls=_mqtt_tls)
        except Exception as _me:
            logger.warning("mqtt_broker_configure_failed",
                           error=str(_me))
    elif _broker_backend == "zeromq":
        try:
            from app.services.zeromq_adapter import configure_zeromq
            _zmq_bind = os.environ.get(
                "HELEN_ZEROMQ_BIND", "tcp://0.0.0.0:5555",
            ).strip()
            _zmq_peers = [
                u.strip() for u in
                (os.environ.get("HELEN_ZEROMQ_PEERS", "") or "").split(",")
                if u.strip()
            ]
            await configure_zeromq(bind_url=_zmq_bind, peer_urls=_zmq_peers)
            logger.info("zeromq_broker_configured",
                        bind=_zmq_bind, peer_count=len(_zmq_peers))
        except Exception as _ze:
            logger.warning("zeromq_broker_configure_failed",
                           error=str(_ze))
    elif _broker_backend == "rabbitmq":
        try:
            from app.services.rabbitmq_adapter import configure_rabbitmq
            _amqp_url = os.environ.get(
                "HELEN_RABBITMQ_URL",
                "amqp://guest:guest@127.0.0.1:5672/",
            ).strip()
            _amqp_exchange = os.environ.get(
                "HELEN_RABBITMQ_EXCHANGE", "helen.events",
            )
            await configure_rabbitmq(
                url=_amqp_url, exchange_name=_amqp_exchange,
            )
            logger.info("rabbitmq_broker_configured",
                        exchange=_amqp_exchange)
        except Exception as _re:
            logger.warning("rabbitmq_broker_configure_failed",
                           error=str(_re))

    # ── Optional SSH tunnels ──────────────────────────────
    if os.environ.get(
        "HELEN_SSH_TUNNELS_ENABLED", "",
    ).lower() in ("1", "true", "yes"):
        try:
            from app.services.ssh_tunnel_manager import (
                configure_ssh_tunnels, parse_tunnel_specs,
            )
            from pathlib import Path as _SSHP
            _ssh_specs = parse_tunnel_specs(
                os.environ.get("HELEN_SSH_TUNNELS", ""),
            )
            _ssh_data_dir = _SSHP(settings.SQLITE_PATH).resolve().parent
            if not _ssh_data_dir.is_absolute():
                _ssh_data_dir = (
                    settings.PROJECT_ROOT / _ssh_data_dir
                ).resolve()
            _ssh_key = str(_ssh_data_dir / "ssh-client.key")
            _ssh_kh = str(_ssh_data_dir / "ssh-known-hosts")
            await configure_ssh_tunnels(
                _ssh_specs,
                key_path=_ssh_key if os.path.exists(_ssh_key) else None,
                known_hosts_path=_ssh_kh if os.path.exists(_ssh_kh) else None,
            )
            logger.info("ssh_tunnels_configured",
                        count=len(_ssh_specs))
        except Exception as _se:
            logger.warning("ssh_tunnels_configure_failed",
                           error=str(_se))

    # ── Online-Mode master gate ───────────────────────────
    # Build the gate first; later blocks register services with it.
    # The gate decides at bootstrap time whether to actually start
    # them, based on the persisted toggle state. Default = OFF, so
    # Helen runs as a pure-LAN deployment unless an admin clicks
    # "Enable Online Mode" in the panel.
    try:
        from app.services.online_mode_gate import (
            configure_online_mode_gate,
        )
        _online_gate = configure_online_mode_gate()
    except Exception as _oge:
        logger.warning("online_mode_gate_configure_failed",
                        error=str(_oge))
        _online_gate = None

    # ── Optional WAN port-forward manager (gated) ─────────
    # Configured at boot but NOT started. The gate calls .start() /
    # .stop() when the operator flips the master toggle.
    try:
        from app.services.wan_port_forward import (
            configure_from_env as _wan_from_env,
            get_wan_portmap as _get_wan,
            shutdown_wan_portmap as _shutdown_wan,
        )
        _wan_mgr = _wan_from_env()
        if _wan_mgr is not None and _online_gate is not None:
            async def _wan_start():
                m = _get_wan()
                if m is not None:
                    await m.start()

            async def _wan_stop():
                await _shutdown_wan()

            _online_gate.register("wan_portmap",
                                    start=_wan_start,
                                    stop=_wan_stop)
            logger.info("wan_portmap_registered_with_gate",
                        external_port=_wan_mgr.external_port,
                        upnp=bool(_wan_mgr.upnp_url))
    except Exception as _we:
        logger.warning("wan_portmap_configure_failed", error=str(_we))

    # ── Optional self-hosted STUN responder ───────────────
    # HELEN_STUN_LISTEN=host:port → pure-Python STUN binding server
    # so LAN clients never need to hit stun.l.google.com.
    try:
        from app.services.stun_responder import (
            configure_from_env as _stun_from_env,
        )
        _stun = _stun_from_env()
        if _stun is not None:
            await _stun.start()
    except Exception as _se:
        logger.warning("stun_responder_configure_failed", error=str(_se))

    # ── Optional mDNS federation autodiscovery ────────────
    # HELEN_FEDERATION_AUTODISCOVER=1 → broadcast & listen on
    # _helen-fed._tcp.local. so clusters bootstrap without manual
    # peer config.
    try:
        from app.services.federation_autodiscovery import (
            configure_from_env as _fad_from_env,
        )
        _fad_from_env(
            my_server_id=_this_sid,
            federation_secret=os.environ.get("FEDERATION_SECRET", ""),
        )
    except Exception as _fae:
        logger.warning("fed_autodiscover_configure_failed",
                        error=str(_fae))

    # ── Optional periodic backup verifier ─────────────────
    # HELEN_BACKUP_VERIFY_ENABLED=1 → restore-into-tempdir checks
    # of the most-recent backup on a fixed cadence.
    try:
        from app.services.backup_verifier import (
            configure_from_env as _bv_from_env,
        )
        from pathlib import Path as _BVPath
        _bv_dir = (settings.PROJECT_ROOT / "data" / "backups").resolve()
        _bv = _bv_from_env(_bv_dir)
        if _bv is not None:
            await _bv.start()
    except Exception as _bve:
        logger.warning("backup_verifier_configure_failed",
                        error=str(_bve))

    # ── Optional federation bandwidth shaper ──────────────
    # HELEN_FEDERATION_BPS_LIMIT=N → per-peer token-bucket cap on
    # federation traffic. Nothing in federation_service has to
    # change; the shaper is consulted by callers via acquire().
    try:
        from app.services.federation_shaper import (
            configure_from_env as _fs_from_env,
        )
        _fs_from_env()
    except Exception as _fse:
        logger.warning("federation_shaper_configure_failed",
                        error=str(_fse))

    # ── Recursive-DNS upstream forwarding (gated) ─────────
    # When the recursive DNS singleton is wired, register a
    # start/stop pair with the gate that toggles the upstream
    # forwarders. With the gate OFF, the resolver still serves
    # *.helen.lan + the blocklist locally, but never reaches out
    # to 9.9.9.9 / 1.1.1.1 etc.
    if _online_gate is not None:
        try:
            from app.core.recursive_dns_singleton import (
                get_recursive_dns,
            )
            _saved_upstreams: list = []

            async def _dns_forwarders_on():
                nonlocal _saved_upstreams
                srv = get_recursive_dns()
                if srv is None:
                    return
                if _saved_upstreams:
                    srv.upstreams = list(_saved_upstreams)

            async def _dns_forwarders_off():
                nonlocal _saved_upstreams
                srv = get_recursive_dns()
                if srv is None:
                    return
                _saved_upstreams = list(srv.upstreams)
                srv.upstreams = []  # block all upstream forwarding

            _online_gate.register("dns_upstream_forward",
                                    start=_dns_forwarders_on,
                                    stop=_dns_forwarders_off)
        except Exception as _de:
            logger.warning("dns_gate_registration_failed",
                            error=str(_de))

    # ── Bootstrap the gate ────────────────────────────────
    # If the persisted state file says ON, start every registered
    # service now. If OFF (default), they stay dormant until the
    # operator clicks "Enable Online Mode" in the panel.
    if _online_gate is not None:
        try:
            await _online_gate.bootstrap()
        except Exception as _gbe:
            logger.warning("online_mode_bootstrap_failed",
                            error=str(_gbe))

    # ── Channel message TTL sweeper ───────────────────────
    # Spawns a background task that walks every channel with a
    # configured cap and deletes messages older than its threshold.
    # Always-on but cheap when no caps are set.
    try:
        from app.services.channel_message_ttl import (
            configure_from_env as _ttl_from_env,
        )
        _ttl_from_env()
    except Exception as _ttle:
        logger.warning("ttl_sweeper_configure_failed", error=str(_ttle))

    # ── Optional gRPC federation transport ────────────────
    # Default federation = HMAC-JSON over HTTP. Operators can opt
    # into a parallel gRPC listener for stricter schemas + native
    # streaming via HELEN_FEDERATION_BACKEND=grpc.
    _fed_backend = os.environ.get(
        "HELEN_FEDERATION_BACKEND", "http",
    ).strip().lower()
    if _fed_backend == "grpc":
        try:
            from app.services.grpc_federation import configure_grpc_federation

            async def _grpc_envelope_handler(env: dict) -> dict:
                # Forward incoming gRPC envelopes through the same
                # route_executor pipeline the HTTP federation uses.
                try:
                    if _executor is not None:
                        # `_executor` is bound earlier in lifespan.
                        # We reconstruct a minimal envelope-shaped
                        # object that the executor's local_deliver
                        # function understands.
                        from app.services.event_envelope import (
                            EventEnvelope as _EvEnv,
                        )
                        try:
                            ev_obj = _EvEnv(**env)
                            await _executor._exec_local_deliver(ev_obj)  # noqa: SLF001
                        except Exception as _e:
                            return {"error": str(_e)[:200]}
                    return {"error": ""}
                except Exception as _e:
                    return {"error": str(_e)[:200]}

            _grpc_port = int(
                os.environ.get("HELEN_GRPC_FEDERATION_PORT", "50051"),
            )
            await configure_grpc_federation(
                bind_host=os.environ.get(
                    "HELEN_GRPC_FEDERATION_HOST", "0.0.0.0",
                ),
                bind_port=_grpc_port,
                envelope_handler=_grpc_envelope_handler,
            )
            logger.info("grpc_federation_configured", port=_grpc_port)
        except Exception as _ge:
            logger.warning("grpc_federation_configure_failed",
                           error=str(_ge))

    # ── Optional WireGuard mesh ───────────────────────────
    _vpn_backend = os.environ.get(
        "HELEN_VPN_BACKEND", "",
    ).strip().lower()
    if _vpn_backend == "wireguard":
        try:
            from app.services.wireguard_manager import (
                configure_wireguard,
            )
            from pathlib import Path as _WGPath
            _wg_data_dir = _WGPath(settings.SQLITE_PATH).resolve().parent
            if not _wg_data_dir.is_absolute():
                _wg_data_dir = (
                    settings.PROJECT_ROOT / _wg_data_dir
                ).resolve()
            _wg_listen = int(
                os.environ.get("HELEN_WG_LISTEN_PORT", "51820"),
            )
            _wg_subnet = os.environ.get(
                "HELEN_WG_MESH_SUBNET", "10.99.0.0/24",
            )
            await configure_wireguard(
                data_dir=str(_wg_data_dir),
                server_id=_this_sid,
                listen_port=_wg_listen,
                mesh_subnet=_wg_subnet,
            )
            logger.info("wireguard_mesh_configured",
                        listen_port=_wg_listen, subnet=_wg_subnet)
        except Exception as _we:
            logger.warning("wireguard_mesh_configure_failed",
                           error=str(_we))

    # ── Audit chain self-verification ─────────────────────
    # Runs every 5 minutes; logs error + creates a crash event if the
    # chain is broken. Cheap (~50 µs/record) so safe to run online.
    _audit_verify_task = None
    try:
        import asyncio as _av_asyncio
        from app.services.audit_chain import get_audit_chain as _get_chain

        async def _audit_verify_loop():
            while True:
                try:
                    await _av_asyncio.sleep(300)   # 5 min
                    chain = _get_chain()
                    if chain is None:
                        continue
                    ok, broken_at, msg = chain.verify()
                    if not ok:
                        logger.error("audit_chain_tamper_detected",
                                     broken_at_seq=broken_at, message=msg)
                        try:
                            from app.services.crash_reporter import get_reporter
                            rep = get_reporter()
                            if rep is not None:
                                rep.capture_event(
                                    "crash",
                                    f"audit_chain_tamper: {msg}",
                                    broken_at_seq=broken_at,
                                )
                        except Exception:
                            pass
                except _av_asyncio.CancelledError:
                    return
                except Exception as _e:
                    logger.warning("audit_chain_verify_iter_failed",
                                   error=str(_e))

        _audit_verify_task = _av_asyncio.create_task(
            _audit_verify_loop(), name="audit-chain-verify",
        )
    except Exception as _e:
        logger.warning("audit_chain_verify_start_failed", error=str(_e))

    # ── Calendar reminder worker ──────────────────────────
    # Polls calendar.db every minute and pushes reminder events over
    # Socket.IO to the creator + every attendee. Uses emit_to_user
    # so any server in the federation can reach the user.
    _reminder_worker = None
    try:
        from app.api.routes.calendar import _get_store as _cal_store
        from app.services.calendar_service import ReminderWorker

        async def _push_reminder(uid: str, payload: dict) -> None:
            try:
                from app.socket.server import emit_to_user as _eu
                await _eu("calendar:reminder", payload, uid)
            except Exception as _pe:
                logger.debug("calendar_reminder_emit_failed",
                             user=uid, error=str(_pe))

        _reminder_worker = ReminderWorker(_cal_store(), _push_reminder)
        await _reminder_worker.start()
        logger.info("calendar_reminder_worker_started")
    except Exception as _re:
        logger.warning("calendar_reminder_worker_start_failed",
                       error=str(_re))

    yield

    # ── Shutdown ────────────────────────────────────────
    logger.info("server_shutting_down")

    # Graceful: notify all connected clients about server shutdown
    try:
        from app.socket.server import sio
        await sio.emit("server:shutdown", {"reason": "Server is restarting"})
    except Exception:
        pass

    # Final saga checkpoint so RUNNING sagas survive into the next
    # process for inspection / resume.
    try:
        from app.services.saga_engine import get_saga_engine
        get_saga_engine()._persist()
    except Exception as _e:
        logger.warning("saga_final_persist_failed", error=str(_e))

    # Stop connectivity orchestrator (tunnel / relay / …). Logged, never raised.
    try:
        from app.services.connectivity import orchestrator as _conn
        await _conn.stop()
    except Exception as _e:
        logger.warning("connectivity_orchestrator_stop_failed", error=str(_e))

    # Snapshot the Kademlia routing table to disk only if persistence
    # is explicitly enabled. Default off keeps shutdown lean.
    try:
        import os as _os_save
        if _os_save.environ.get("HELEN_DHT_PERSIST", "").lower() in {"1", "true", "yes", "on"}:
            from app.services.dht_kademlia import save_routing_table_to_disk
            save_routing_table_to_disk()
    except Exception as _e:
        logger.warning("kademlia_save_on_shutdown_failed", error=str(_e))

    # Stop calendar reminder worker (cancels its 60s loop).
    try:
        if _reminder_worker is not None:
            await _reminder_worker.stop()
    except Exception as _e:
        logger.debug("calendar_reminder_worker_stop_failed", error=str(_e))

    # Stop optional alternate transports — best-effort.
    try:
        from app.services.nats_adapter import shutdown_nats
        await shutdown_nats()
    except Exception as _e:
        logger.debug("nats_shutdown_failed", error=str(_e))
    try:
        from app.services.mqtt_adapter import shutdown_mqtt
        await shutdown_mqtt()
    except Exception as _e:
        logger.debug("mqtt_shutdown_failed", error=str(_e))
    try:
        from app.services.grpc_federation import shutdown_grpc_federation
        await shutdown_grpc_federation()
    except Exception as _e:
        logger.debug("grpc_federation_shutdown_failed", error=str(_e))
    try:
        from app.services.wireguard_manager import shutdown_wireguard
        await shutdown_wireguard()
    except Exception as _e:
        logger.debug("wireguard_shutdown_failed", error=str(_e))
    try:
        from app.services.zeromq_adapter import shutdown_zeromq
        await shutdown_zeromq()
    except Exception as _e:
        logger.debug("zeromq_shutdown_failed", error=str(_e))
    try:
        from app.services.rabbitmq_adapter import shutdown_rabbitmq
        await shutdown_rabbitmq()
    except Exception as _e:
        logger.debug("rabbitmq_shutdown_failed", error=str(_e))
    try:
        from app.services.ssh_tunnel_manager import shutdown_ssh_tunnels
        await shutdown_ssh_tunnels()
    except Exception as _e:
        logger.debug("ssh_tunnels_shutdown_failed", error=str(_e))
    try:
        from app.services.wan_port_forward import shutdown_wan_portmap
        await shutdown_wan_portmap()
    except Exception as _e:
        logger.debug("wan_portmap_shutdown_failed", error=str(_e))
    try:
        from app.services.stun_responder import shutdown_stun_responder
        await shutdown_stun_responder()
    except Exception as _e:
        logger.debug("stun_responder_shutdown_failed", error=str(_e))
    try:
        from app.services.federation_autodiscovery import (
            stop_federation_autodiscovery,
        )
        stop_federation_autodiscovery()
    except Exception as _e:
        logger.debug("fed_autodiscover_shutdown_failed", error=str(_e))
    try:
        from app.services.backup_verifier import shutdown_backup_verifier
        await shutdown_backup_verifier()
    except Exception as _e:
        logger.debug("backup_verifier_shutdown_failed", error=str(_e))
    try:
        from app.services.federation_shaper import shutdown_federation_shaper
        shutdown_federation_shaper()
    except Exception as _e:
        logger.debug("federation_shaper_shutdown_failed", error=str(_e))
    try:
        from app.services.online_mode_gate import (
            get_online_mode_gate, reset_online_mode_gate,
        )
        _g = get_online_mode_gate()
        if _g is not None:
            await _g.shutdown()
        reset_online_mode_gate()
    except Exception as _e:
        logger.debug("online_mode_gate_shutdown_failed", error=str(_e))
    try:
        from app.services.channel_message_ttl import shutdown as _ttl_shutdown
        await _ttl_shutdown()
    except Exception as _e:
        logger.debug("ttl_sweeper_shutdown_failed", error=str(_e))

    # Stop audit writer (drain queue if possible)
    from app.core.audit import stop_audit_writer
    await stop_audit_writer()

    # Stop automated backup scheduler
    try:
        from app.services import backup_scheduler
        await backup_scheduler.stop()
    except Exception as e:
        logger.warning("auto_backup_stop_failed", error=str(e))

    # Drain & stop batched participant writer before closing DB pool.
    try:
        from app.services.call_participant_batcher import call_participant_batcher
        await call_participant_batcher.stop()
    except Exception as _e:
        logger.warning("participant_batcher_stop_failed", error=str(_e))

    # Signal webhook inner loop first so the supervisor wrapping it can
    # exit cleanly (the wrapper awaits WebhookService.run_dispatch_loop
    # which itself checks _webhook_stop).
    try:
        _webhook_stop.set()
    except Exception:
        pass

    # Cancel every leader-gated background task uniformly. Each
    # supervisor task owns its inner subprocess (if any) and releases
    # the lease in its finally-block, so we just need to cancel and
    # wait for them to unwind.
    _leader_gated_tasks = [
        ("call_orphan_sweeper",          _call_sweep_task),
        ("upload_gc",                    _upload_gc_task),
        ("status_message_expiry",        _status_msg_task),
        ("channel_mute_expiry",          _mute_task),
        ("scheduled_message_dispatcher", _scheduled_task),
        ("webhook_dispatcher",           _webhook_task),
        ("poll_expiry",                  _poll_task),
        ("dlq_reaper",                   _dlq_task),
        ("group_file_sweeper",           _group_file_task),
    ]
    # Add WAL task only if it was created (SQLite only).
    try:
        _leader_gated_tasks.append(("wal_checkpoint", _wal_task))  # type: ignore[name-defined]
    except NameError:
        pass

    for _name, _t in _leader_gated_tasks:
        try:
            _t.cancel()
        except Exception:
            pass
    for _name, _t in _leader_gated_tasks:
        try:
            await asyncio.wait_for(_t, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as _e:
            logger.warning("leader_task_shutdown_error",
                           name=_name, error=str(_e))

    # Cancel heartbeat cleanup (not leader-gated).
    try:
        _heartbeat_task.cancel()
        try:
            await asyncio.wait_for(_heartbeat_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    except Exception:
        pass

    await udp_broadcast.stop()
    await mdns_service.stop()
    await udp_listener_service.stop()
    try:
        from app.services.router_client import stop_router_client
        await stop_router_client()
    except Exception as _e:
        logger.warning("router_client_stop_failed", error=str(_e))
    try:
        from app.services.relay_worker import relay_manager
        await relay_manager.stop()
    except Exception as _e:
        logger.warning("relay_manager_stop_failed", error=str(_e))
    try:
        from app.services.federated_presence import federated_presence
        await federated_presence.stop_resync_loop()
    except Exception as _e:
        logger.warning("federated_presence_stop_failed", error=str(_e))

    # ── Distributed services shutdown ──
    try:
        from app.socket.server_fabric_handlers import get_fabric_subscribers as _gfs
        f = _gfs()
        if f is not None:
            await f.stop()
    except Exception as _e:
        logger.warning("fabric_subscribers_stop_failed", error=str(_e))
    try:
        from app.services.event_ack_manager import get_ack_manager as _gam
        await _gam().stop()
    except Exception as _e:
        logger.warning("ack_manager_stop_failed", error=str(_e))
    try:
        from app.services.broker_client import get_broker as _gb
        b = _gb()
        if b is not None:
            await b.stop()
    except Exception as _e:
        logger.warning("broker_stop_failed", error=str(_e))
    try:
        from app.services.trace_collector_service import trace_collector as _tc
        await _tc.stop()
    except Exception as _e:
        logger.warning("trace_collector_stop_failed", error=str(_e))
    try:
        from app.services.load_monitor import get_load_monitor as _glm
        m = _glm()
        if m is not None:
            await m.stop()
    except Exception as _e:
        logger.warning("load_monitor_stop_failed", error=str(_e))
    try:
        from app.services.origin_election_service import get_origin_election_service as _goes
        await _goes().stop()
    except Exception as _e:
        logger.warning("origin_election_stop_failed", error=str(_e))
    try:
        from app.services.server_registry_service import get_registry_service as _grs
        reg = _grs()
        await reg.stop()
        await reg.deregister()
    except Exception as _e:
        logger.warning("server_registry_stop_failed", error=str(_e))
    try:
        from app.services.distributed_presence_service import get_presence_service as _gps
        await _gps().stop()
    except Exception as _e:
        logger.warning("distributed_presence_stop_failed", error=str(_e))
    try:
        from app.services.control_plane import ControlPlane
        await ControlPlane.instance().stop()
    except Exception as _e:
        logger.warning("control_plane_stop_failed", error=str(_e))
    await engine.dispose()
    logger.info("server_stopped")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title=settings.SERVER_NAME,
        description="LAN-only communication platform backend",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # ── Security Middleware (order matters: outermost first) ──
    from app.core.middleware import (
        GlobalRateLimitMiddleware,
        RequestIdMiddleware,
        RequestLatencyMiddleware,
        RequestSizeLimitMiddleware,
        RouterRequiredMiddleware,
        SecurityHeadersMiddleware,
    )

    # 0. Latency tracker — outermost so the wall-clock duration includes
    #    every other middleware's cost. Records into `latency_tracker`
    #    which the /api/metrics endpoint exposes as Prometheus histograms.
    app.add_middleware(RequestLatencyMiddleware)
    # 0.5 Router-required — runs early so rejected requests don't burn
    #     CPU on parsing JWTs / building DB sessions. No-op unless
    #     HELEN_REQUIRE_ROUTER=1 is set.
    app.add_middleware(RouterRequiredMiddleware)
    # 1. Request ID — runs after latency so the X-Request-ID header is
    #    available in observability tooling.
    app.add_middleware(RequestIdMiddleware)
    # 2. Security headers — inject on every response
    app.add_middleware(SecurityHeadersMiddleware)
    # 3. Request size limit — reject oversized payloads early
    app.add_middleware(RequestSizeLimitMiddleware, max_size_bytes=115_343_360)
    # 4. Global rate limit — throttle every endpoint keyed by user_id or IP.
    #    Runs AFTER size/ID so 429s still carry a request id and oversized
    #    payloads are rejected before we bother to parse a JWT.
    app.add_middleware(
        GlobalRateLimitMiddleware,
        enabled=settings.RATE_LIMIT_GLOBAL_ENABLED,
        trust_lan=settings.RATE_LIMIT_TRUST_LAN,
    )

    # ── CORS ────────────────────────────────────────────
    # Three-machine LAN scenario: Helen-Admin may run on 192.168.1.2,
    # Helen-Server on 192.168.1.3, and Helen.exe on 192.168.1.4 — each
    # sends a different Origin, all legitimate. Hard-coding 127.0.0.1
    # locks admin+client to the same box as the server. We expand to a
    # regex that matches:
    #   * http(s)://localhost | 127.0.0.1 | [::1]
    #   * http(s)://<any-IPv4>[:<port>]           ← LAN hosts
    #   * http(s)://<label>.local[:<port>]         ← mDNS hostnames
    #   * http(s)://<bare-hostname>[:<port>]       ← Windows NetBIOS
    #   * app://.                                   ← Electron packaged
    # Origin header "null" (Electron file:// pages, some privacy modes)
    # is whitelisted via explicit allow_origins — regex can't match it
    # because `null` is not a URL.
    # NOTE: bare NetBIOS-style hostnames are intentionally NOT in this
    # regex — they were too permissive (any single-label name pretending
    # to be a LAN peer would pass). For mDNS-style hostnames use the
    # *.local branch; for arbitrary corporate FQDNs use the IPv4 form.
    LAN_ORIGIN_REGEX = (
        r"^(https?://("
        r"localhost|127\.0\.0\.1|\[::1\]|"
        r"\d+\.\d+\.\d+\.\d+|"
        r"[a-zA-Z0-9-]+\.local"
        r")(:[0-9]+)?|app://\.|null)$"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["null"],        # Electron file:// pages send Origin: null
        allow_origin_regex=LAN_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "Accept",
        ],
        expose_headers=["X-Request-ID"],
        max_age=3600,
    )

    # ── Exception Handlers ──────────────────────────────
    register_exception_handlers(app)

    # ── REST API Routes ─────────────────────────────────
    from app.api.routes import api_router
    app.include_router(api_router)

    # Network Topology Visualizer — admin panel REST + WS.
    # Router declares its own ``/api/admin`` prefix so it mounts directly on
    # the app (matches admin_monitoring's pattern).
    try:
        from app.api.routes.admin_topology import router as admin_topology_router
        app.include_router(admin_topology_router)
        logger.info("admin_topology_router_registered")
    except Exception as _e:
        logger.warning("admin_topology_router_register_failed", error=str(_e))

    # Federation Health Map — admin panel REST + WS.
    # Router declares its own ``/api/admin/federation`` prefix; the
    # protocol-layer admin_federation_v2 router lives under
    # ``/api/admin/federation/v2`` and is loaded separately.
    try:
        from app.api.routes.admin_federation import (
            router as admin_federation_router,
        )
        app.include_router(admin_federation_router)
        logger.info("admin_federation_router_registered")
    except Exception as _e:
        logger.warning(
            "admin_federation_router_register_failed", error=str(_e),
        )

    # ── Tenancy + RBAC + Billing Portal (admin) ─────────────────────
    # Mounts under /api/admin alongside the existing admin_billing router.
    # Adds tenant CRUD, RBAC role/permission/user management, Ed25519
    # license signing, plan audit history, on-demand invoice generation.
    try:
        from app.api.routes.admin_tenancy_portal import (
            router as admin_tenancy_portal_router,
        )
        app.include_router(admin_tenancy_portal_router)
        logger.info("admin_tenancy_portal_router_registered")
    except Exception as _e:
        logger.warning(
            "admin_tenancy_portal_router_register_failed", error=str(_e),
        )

    # ── Unified Monitoring Dashboard (admin) ────────────────────────
    # Router declares its own ``/api/admin`` prefix so it mounts directly
    # on the app (parallel to admin_topology/admin_tenancy_portal). Exposes
    # service health, metrics fan-out, alert rules, live WebSocket stream.
    try:
        from app.api.routes.admin_monitoring import (
            router as admin_monitoring_router,
        )
        app.include_router(admin_monitoring_router)
        logger.info("admin_monitoring_router_registered")
    except Exception as _e:
        logger.warning(
            "admin_monitoring_router_register_failed", error=str(_e),
        )

    # ── Plugin Marketplace / Sandbox (admin) ────────────────────────
    # Router declares its own ``/api/admin/plugins`` prefix so it mounts
    # directly on the app. Endpoints were appended to the existing
    # admin_plugins.py module (catalog, install, ratings, sandbox status,
    # quotas, audit). Mounted on ``app`` (not ``api_router``) to avoid the
    # ``/api`` prefix being added twice.
    try:
        from app.api.routes.admin_plugins import (
            router as admin_plugins_router,
        )
        app.include_router(admin_plugins_router)
        logger.info("admin_plugins_router_registered")
    except Exception as _e:
        logger.warning(
            "admin_plugins_router_register_failed", error=str(_e),
        )

    # ── Compliance / eDiscovery Workbench (admin) ──────────────────
    # Router declares its own ``/api/admin/compliance`` prefix; mounted
    # directly on the app. Endpoints were appended to the existing
    # admin_compliance.py module (GDPR exports, DSAR processing, legal
    # holds, retention policies, eDiscovery query engine).
    try:
        from app.api.routes.admin_compliance import (
            router as admin_compliance_router,
        )
        app.include_router(admin_compliance_router)
        logger.info("admin_compliance_router_registered")
    except Exception as _e:
        logger.warning(
            "admin_compliance_router_register_failed", error=str(_e),
        )

    # Public HTML page for phone pairing (no /api prefix — user types/scans this).
    from app.api.routes.pair import public_router as pair_public_router
    app.include_router(pair_public_router)

    # Browser landing page for channel invite codes — opens at
    # /join/<code>. Read-only HTML response; the actual join
    # happens once the user opens Helen Desktop and POSTs to
    # /api/channels/join-by-code with the same code.
    from app.api.routes.join_page import router as join_page_router
    app.include_router(join_page_router)

    # ── /mobile/ and /admin-mobile/ — phone-sized web clients ──
    # /mobile/       → iOS/web-simulator/   (end-user client)
    # /admin-mobile/ → iOS-Admin/web-simulator/ (operator console)
    # Both are static HTML/CSS/JS apps that talk to the same Helen REST
    # + Socket.IO backend. Any phone on the LAN opens them in Safari;
    # no native install required. Sources live in the repo; PyInstaller
    # bundles copies inside the frozen exe (see spec datas).
    import sys
    from pathlib import Path
    from fastapi.staticfiles import StaticFiles

    def _mount_simulator(url_prefix: str, rel_src: tuple[str, str]) -> None:
        candidates: list[Path] = []
        _mp = getattr(sys, "_MEIPASS", None)
        if _mp:
            candidates.append(Path(_mp) / rel_src[0] / rel_src[1])
        candidates.append(
            Path(__file__).resolve().parents[2] / rel_src[0] / rel_src[1]
        )
        for cand in candidates:
            if cand.is_dir() and (cand / "index.html").is_file():
                app.mount(
                    url_prefix,
                    StaticFiles(directory=str(cand), html=True),
                    name=url_prefix.strip("/").replace("-", "_"),
                )
                logger.info(
                    "simulator_mounted", prefix=url_prefix, path=str(cand)
                )
                return
        logger.warning(
            "simulator_not_found",
            prefix=url_prefix,
            tried=[str(p) for p in candidates],
        )
    _mount_simulator("/mobile", ("iOS", "web-simulator"))
    _mount_simulator("/admin-mobile", ("iOS-Admin", "web-simulator"))

    # ── Mount Socket.IO at /socket.io/ ──
    # python-socketio's sio.attach() doesn't play well with FastAPI/Starlette
    # routers (it expects aiohttp/sanic). We instead wrap the FastAPI app in a
    # `socketio.ASGIApp` after construction by stashing the sio reference on
    # the FastAPI state — uvicorn will get the wrapped app via `get_asgi_app()`.
    try:
        from app.socket.server import sio as _sio
        import socketio as _socketio_lib
        app.state.sio = _sio
        app.state.asgi_app = _socketio_lib.ASGIApp(_sio, other_asgi_app=app, socketio_path="/socket.io")
        logger.info("socketio_asgi_built", path="/socket.io")
    except Exception as _e:
        logger.warning("socketio_attach_failed", error=str(_e))

    return app


# Module-level FastAPI app instance — what uvicorn (and smoke/E2E) import.
# Built defensively so a partial environment still exposes the symbol.
try:
    app = create_app()
except Exception as _e:  # pragma: no cover
    try:
        import structlog as _struct
        _struct.get_logger("app.main").error("create_app_failed", error=str(_e))
    except Exception:
        import logging as _lg
        _lg.getLogger("app.main").error("create_app_failed: %s", _e)
    from fastapi import FastAPI as _FastAPI
    app = _FastAPI(title="Helen (degraded)", version="1.0.0")

    @app.get("/api/admin/health")
    async def _degraded_health():
        return {"status": "degraded", "error": str(_e)}


def get_asgi_app():
    """Return the Socket.IO-wrapped ASGI app if available, else the bare FastAPI app.

    Use this in production: ``uvicorn app.main:get_asgi_app --factory``
    or simply import the module-level ``asgi_app`` symbol below.
    """
    try:
        return app.state.asgi_app
    except Exception:
        return app


try:
    asgi_app = app.state.asgi_app  # Socket.IO-wrapped, preferred for uvicorn
except Exception:
    asgi_app = app
