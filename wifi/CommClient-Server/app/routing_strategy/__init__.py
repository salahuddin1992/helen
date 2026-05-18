"""Advanced Adaptive Routing Strategy — pluggable composable router.

Public entry point for the strategy package. Each strategy lives in
its own module and can be enabled / disabled / re-weighted via
``strategy_config``. The manager composes them into a single
``RouteDecision``.

Usage
-----
    from app.routing_strategy import routing_strategy_manager
    decision = await routing_strategy_manager.route(
        target_node_id="...", method="GET", path="/api/x",
    )

The package is *additive* alongside ``app/services/multipath_router``
— that file remains the lower-level send/relay primitive; this
package is the higher-level policy orchestrator.
"""

from app.routing_strategy.routing_context import RoutingContext            # noqa: F401
from app.routing_strategy.route_candidate import RouteCandidate            # noqa: F401
from app.routing_strategy.route_decision import RouteDecision              # noqa: F401
from app.routing_strategy.routing_policy import RoutingPolicy              # noqa: F401
from app.routing_strategy.routing_strategy_manager import (                # noqa: F401
    RoutingStrategyManager,
    get_strategy_manager,
    start_strategy_manager,
    stop_strategy_manager,
)
