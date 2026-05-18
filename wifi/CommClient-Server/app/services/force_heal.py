"""Force-heal — operator-triggered partition recovery.

Wraps the existing self-healing primitives so an admin can:

  1. Run an immediate gossip cycle.
  2. Run a full state reconciliation.
  3. Run an anti-entropy round.
  4. Re-evaluate partition state.

Used when the automatic watchdog hasn't kicked in fast enough.
"""

from __future__ import annotations

import asyncio
import time

from app.core.logging import get_logger

logger = get_logger(__name__)


async def force_heal_now() -> dict:
    """Run all four healing primitives + return per-step results."""
    started = time.time()
    out: dict = {"ts": started}

    async def _safe(name: str, coro) -> dict:
        try:
            await coro
            return {"name": name, "ok": True}
        except Exception as e:
            return {"name": name, "ok": False, "error": str(e)[:120]}

    steps: list[dict] = []

    # 1. Gossip
    try:
        from app.services.anti_entropy import _cycle as ae_cycle
        steps.append(await _safe("anti_entropy", ae_cycle()))
    except ImportError:
        steps.append({"name": "anti_entropy", "ok": False, "error": "missing"})

    # 2. State reconciliation
    try:
        from app.services.state_reconciliation import _reconcile_once
        steps.append(await _safe("state_reconciliation", _reconcile_once()))
    except ImportError:
        steps.append({"name": "state_reconciliation", "ok": False,
                      "error": "missing"})

    # 3. Bandwidth probe (refresh)
    try:
        from app.services.bandwidth_probe import _probe_cycle
        steps.append(await _safe("bandwidth_probe", _probe_cycle()))
    except ImportError:
        steps.append({"name": "bandwidth_probe", "ok": False,
                      "error": "missing"})

    # 4. Re-check partition state
    try:
        from app.services.partition_detector import _check_once
        steps.append(await _safe("partition_check", _check_once()))
    except ImportError:
        steps.append({"name": "partition_check", "ok": False,
                      "error": "missing"})

    out["steps"] = steps
    out["elapsed_ms"] = round((time.time() - started) * 1000.0, 2)
    out["all_ok"] = all(s.get("ok") for s in steps)
    logger.info("force_heal_completed", **{
        "all_ok": out["all_ok"],
        "elapsed_ms": out["elapsed_ms"],
    })
    return out
