"""Routing strategy manager — orchestrator + public entry point.

Lifecycle of a single ``route()`` call:

    1. Build RoutingContext from caller args + live cluster state.
    2. Discover candidate routes via multipath_router.
    3. Wrap each Route in a RouteCandidate.
    4. Apply route_constraints (hard rejection).
    5. Run the configured strategy chain — each annotates contributions.
    6. Run the scoring engine to compose final weights.
    7. Run the selector to pick primary + fallbacks.
    8. Build a RouteDecision, fire route_events, record metrics.

The manager is also the place where periodic refresh + health
maintenance lives, mirroring the lifecycle of the other singletons
in ``app/services/``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Optional

from app.core.logging import get_logger

from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_constraints import apply_constraints
from app.routing_strategy.route_decision import RouteDecision
from app.routing_strategy.route_events import emit
from app.routing_strategy.route_metrics import get_metrics
from app.routing_strategy.route_scoring_engine import score_all
from app.routing_strategy.route_selector import (
    select_top_k,
    split_primary_and_fallbacks,
)
from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.routing_policy import (
    RoutingPolicy,
    policy_default,
    policy_for_context,
)
from app.routing_strategy.strategy_config import get_config
from app.routing_strategy.strategy_exceptions import (
    AllRoutesRejectedError,
    NoCandidatesError,
)

logger = get_logger(__name__)


# ── Strategy registry ──────────────────────────────────────────


def _strategies() -> dict[str, Callable[[RoutingContext, list[RouteCandidate]], None]]:
    """Map strategy_name → evaluate(ctx, candidates) function.

    Lazy-imported so the manager's own import is cheap and so
    individual strategies can be hot-swapped without restarting.
    """
    from app.routing_strategy import (
        trust_aware_strategy, nat_aware_strategy, relay_strategy,
        proxy_strategy, federation_strategy, load_balancing_strategy,
        multipath_strategy, failover_strategy, adaptive_strategy,
    )
    return {
        trust_aware_strategy.NAME:    trust_aware_strategy.evaluate,
        nat_aware_strategy.NAME:      nat_aware_strategy.evaluate,
        relay_strategy.NAME:          relay_strategy.evaluate,
        proxy_strategy.NAME:          proxy_strategy.evaluate,
        federation_strategy.NAME:     federation_strategy.evaluate,
        load_balancing_strategy.NAME: load_balancing_strategy.evaluate,
        multipath_strategy.NAME:      multipath_strategy.evaluate,
        failover_strategy.NAME:       failover_strategy.evaluate,
        adaptive_strategy.NAME:       adaptive_strategy.evaluate,
    }


# ── Manager singleton ─────────────────────────────────────────


class RoutingStrategyManager:
    _singleton: "RoutingStrategyManager | None" = None

    def __init__(self) -> None:
        self._policy: RoutingPolicy = policy_default()
        self._loop_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._last_decision: Optional[RouteDecision] = None

    @classmethod
    def instance(cls) -> "RoutingStrategyManager":
        if cls._singleton is None:
            cls._singleton = RoutingStrategyManager()
        return cls._singleton

    # ── Policy management ───────────────────────────────────

    def set_policy(self, policy: RoutingPolicy) -> None:
        self._policy = policy
        emit("policy.changed", {"policy": policy.to_dict()})

    @property
    def policy(self) -> RoutingPolicy:
        return self._policy

    # ── Helpers to gather live state ────────────────────────

    def _gather_live_state(self) -> dict:
        is_majority = True
        bp = "normal"
        rendezvous = False
        try:
            from app.services.partition_detector import get_partition_state
            is_majority = get_partition_state().is_majority()
        except Exception:
            pass
        try:
            from app.services.backpressure import get_backpressure
            bp = get_backpressure().snapshot().get("level", "normal")
        except Exception:
            pass
        import os
        rendezvous = bool(os.environ.get("HELEN_RENDEZVOUS_HOST"))
        self_id = ""
        cluster = "default"
        try:
            from app.services.discovery_service import get_server_id
            self_id = get_server_id() or ""
        except Exception:
            pass
        try:
            from app.core.config import get_settings
            cluster = get_settings().COMMCLIENT_CLUSTER_ID
        except Exception:
            pass
        return {
            "self_node_id":         self_id,
            "cluster_id":           cluster,
            "is_majority":          is_majority,
            "backpressure_level":   bp,
            "rendezvous_available": rendezvous,
        }

    async def _discover_candidates(self, target_id: str) -> list[RouteCandidate]:
        try:
            from app.services.multipath_router import discover_routes
        except ImportError:
            return []
        routes = await discover_routes(target_id)
        out = []
        for i, r in enumerate(routes):
            out.append(RouteCandidate(
                route=r,
                candidate_id=f"c{i}-{uuid.uuid4().hex[:6]}",
            ))
        return out

    # ── Public entry point ───────────────────────────────────

    async def route(
        self,
        target_node_id: str,
        *,
        method: str = "GET",
        path: str = "/",
        body: Any = None,
        essential: bool = False,
        deadline_sec: float = 5.0,
        max_attempts: Optional[int] = None,
    ) -> RouteDecision:
        cfg = get_config()
        live = self._gather_live_state()
        ctx = RoutingContext(
            target_node_id=target_node_id,
            method=method, path=path, body=body,
            essential=essential,
            deadline_sec=deadline_sec,
            max_attempts=max_attempts or cfg.max_attempts,
            request_id=uuid.uuid4().hex,
            **live,
        )

        policy = policy_for_context(
            is_majority=ctx.is_majority,
            backpressure_level=ctx.backpressure_level,
            rendezvous_available=ctx.rendezvous_available,
        )

        decision = RouteDecision(
            target_node_id=target_node_id,
            policy_name=policy.name,
            strategies=list(policy.strategy_names),
        )

        # 1. Discover candidates.
        candidates = await self._discover_candidates(target_node_id)
        if not candidates:
            decision.notes.append("no_candidates")
            decision.mark_finished()
            self._last_decision = decision
            get_metrics().record_decision(
                has_route=False, duration_ms=decision.duration_ms(),
            )
            emit("decision.failed",
                 {"reason": "no_candidates", "target": target_node_id})
            return decision

        # 2. Constraints (hard gates).
        candidates = apply_constraints(ctx, candidates)

        # 3. Run strategy chain.
        registry = _strategies()
        metrics = get_metrics()
        for name in policy.strategy_names:
            fn = registry.get(name)
            if fn is None:
                decision.notes.append(f"unknown_strategy:{name}")
                continue
            t0 = time.time()
            try:
                fn(ctx, candidates)
            except Exception as e:
                decision.notes.append(f"strategy_failed:{name}:{e}")
                logger.warning(
                    "strategy_evaluate_failed",
                    strategy=name, error=str(e),
                )
            metrics.record_strategy(
                name, (time.time() - t0) * 1000.0,
            )

        # 4. Scoring engine.
        score_all(candidates, cfg)

        # 5. Selector.
        try:
            ordered = select_top_k(candidates, k=cfg.top_k)
            primary, fallbacks = split_primary_and_fallbacks(ordered)
            decision.primary = primary
            decision.fallbacks = fallbacks
        except AllRoutesRejectedError:
            decision.notes.append("all_rejected")
            decision.rejected = candidates

        decision.mark_finished()
        self._last_decision = decision

        # 6. Metrics + events.
        metrics.record_decision(
            has_route=decision.has_route,
            duration_ms=decision.duration_ms(),
            primary_route_type=decision.primary.route_type if decision.primary else None,
        )
        emit(
            "decision.made" if decision.has_route else "decision.failed",
            decision.to_dict(),
        )
        return decision

    # ── Background refresh loop (light) ─────────────────────

    async def _refresh_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info(
            "routing_strategy_manager_started",
            interval_sec=cfg.refresh_interval_sec,
        )
        try:
            while self._running:
                snap = get_metrics().snapshot()
                emit("manager.heartbeat", {
                    "policy": self._policy.name,
                    "ts": time.time(),
                    "decisions": snap.get("decisions_total", 0),
                    "no_route_rate": snap.get("no_route_rate", 0.0),
                })
                # Slow adaptation: if the no-route rate spikes above
                # 30% over the rolling window, broaden the policy by
                # appending federation + multipath strategies — gives
                # the next decisions more candidates to work with.
                # Reverts automatically once the rate drops back below
                # 10% (hysteresis).
                try:
                    nr = float(snap.get("no_route_rate", 0.0) or 0.0)
                    names = list(self._policy.strategy_names or [])
                    broadened = "federation" in names and "multipath" in names
                    if nr >= 0.30 and not broadened:
                        widened = list(names)
                        for s in ("federation", "multipath"):
                            if s not in widened:
                                widened.append(s)
                        self._policy = self._policy.with_strategies(widened)
                        self._policy.name = self._policy.name + "+broadened"
                        emit("policy.broadened", {
                            "reason": "no_route_rate_high",
                            "no_route_rate": nr,
                            "strategies": widened,
                        })
                    elif nr <= 0.10 and self._policy.name.endswith("+broadened"):
                        self._policy = policy_default()
                        emit("policy.restored", {
                            "no_route_rate": nr,
                            "strategies": self._policy.strategy_names,
                        })
                except Exception as e:
                    logger.debug("manager_adapt_skipped", error=str(e))
                await asyncio.sleep(cfg.refresh_interval_sec)
        finally:
            logger.info("routing_strategy_manager_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._refresh_loop(),
                name="routing-strategy-manager",
            )
        except RuntimeError:
            logger.warning("routing_strategy_manager_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    # ── Diagnostics ─────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "policy":   self._policy.to_dict(),
            "metrics":  get_metrics().snapshot(),
            "last_decision": (
                self._last_decision.to_dict()
                if self._last_decision else None
            ),
            "available_strategies": sorted(_strategies().keys()),
        }


def get_strategy_manager() -> RoutingStrategyManager:
    return RoutingStrategyManager.instance()


def start_strategy_manager() -> None:
    get_strategy_manager().start()


def stop_strategy_manager() -> None:
    get_strategy_manager().stop()
