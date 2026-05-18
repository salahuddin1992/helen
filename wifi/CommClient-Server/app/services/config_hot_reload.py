"""Config hot reload — re-read env-tunable settings without restart.

Each subsystem keeps a singleton config (frozen dataclass that reads
env vars on init). This module triggers ``reload_config()`` on every
known subsystem so an admin can change ``HELEN_*`` env vars and have
them applied immediately.

Subsystems known to support reload:

  * routing_strategy.strategy_config
  * distributed_system.distributed_config
  * monitoring.monitoring_config
  * p2p.p2p_config
  * overlay.overlay_config
  * resilience.resilience_config
  * nat.nat_config
  * service_discovery.discovery_config

Modules that don't expose a reload function are silently skipped.
"""

from __future__ import annotations

from typing import Callable

from app.core.logging import get_logger

logger = get_logger(__name__)


# (module_path, attr_name) — attr_name is the reload function name
# that lives in the config module of each subsystem.
_RELOADERS: list[tuple[str, str]] = [
    ("app.routing_strategy.strategy_config",     "reload_config"),
    ("app.distributed_system.distributed_config", "reload_config"),
    ("app.monitoring.monitoring_config",         "reload_config"),
    ("app.p2p.p2p_config",                       "reload_config"),
    ("app.overlay.overlay_config",               "reload_config"),
    ("app.resilience.resilience_config",         "reload_config"),
    ("app.nat.nat_config",                       "reload_config"),
    ("app.service_discovery.discovery_config",   "reload_config"),
]


def reload_all() -> dict:
    """Run every known subsystem's reload. Returns per-module result."""
    results: dict[str, dict] = {}
    for module_path, attr in _RELOADERS:
        entry: dict = {"module": module_path}
        try:
            mod = __import__(module_path, fromlist=[attr])
            fn: Callable = getattr(mod, attr, None)  # type: ignore[assignment]
            if fn is None:
                entry["ok"] = False
                entry["reason"] = "no_reload_attr"
            else:
                fn()
                entry["ok"] = True
        except ImportError as e:
            entry["ok"] = False
            entry["reason"] = f"import:{e}"
        except Exception as e:
            entry["ok"] = False
            entry["reason"] = str(e)[:120]
            logger.warning("config_reload_failed",
                           module=module_path, error=str(e)[:120])
        results[module_path.split(".")[-2]] = entry
    return {
        "reloaded":     sum(1 for v in results.values() if v.get("ok")),
        "total":        len(results),
        "per_module":   results,
    }


def status() -> dict:
    return {
        "registered_reloaders": [
            f"{mp}.{attr}" for mp, attr in _RELOADERS
        ],
        "count": len(_RELOADERS),
    }
