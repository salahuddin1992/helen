"""Plugin loader — extensibility hooks for messages / calls / files.

Plugins are Python files in ``HELEN_PLUGIN_DIR`` (default
``data/plugins/``). Each file exposes one or more handler functions
that the loader registers against named hook channels. Handlers are
called sync; they should be fast (audit, transform, deny) — not
blocking I/O.

Hook signature::

    def on_message(payload: dict) -> dict | None:
        # Return modified payload to forward, None to drop, raise to deny.

Available hooks (mirroring the internal event names):

  * ``message.before_send``
  * ``message.after_send``
  * ``call.started``
  * ``call.ended``
  * ``file.uploaded``
  * ``file.shared``
  * ``service.registered``
  * ``service.expired``

Each plugin module declares its handlers via a top-level ``HOOKS``
dict::

    HOOKS = {
        "message.before_send": censor_profanity,
        "call.started":        log_call_start,
    }

The loader scans the directory once at startup; reload via
``/api/admin/peers/plugins/reload``.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)


HookFn = Callable[[dict], Any]


def _plugin_dir() -> Path:
    raw = os.environ.get("HELEN_PLUGIN_DIR", "")
    if raw:
        return Path(raw)
    return (Path(os.environ.get("COMMCLIENT_DATA_DIR",
                  str(Path(__file__).resolve().parents[2] / "data")))
            / "plugins")


class PluginRegistry:
    _singleton: "PluginRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hooks: dict[str, list[tuple[str, HookFn]]] = defaultdict(list)
        self._loaded_modules: list[dict] = []
        self._scan_errors: list[dict] = []

    @classmethod
    def instance(cls) -> "PluginRegistry":
        if cls._singleton is None:
            cls._singleton = PluginRegistry()
        return cls._singleton

    # ── Lifecycle ─────────────────────────────────────────

    def load_all(self) -> dict:
        """Re-scan the plugin directory. Existing handlers are dropped
        before reload."""
        with self._lock:
            self._hooks.clear()
            self._loaded_modules.clear()
            self._scan_errors.clear()

        pdir = _plugin_dir()
        if not pdir.is_dir():
            return {"loaded": 0, "errors": 0,
                    "reason": "plugin_dir_missing", "dir": str(pdir)}

        for path in sorted(pdir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._load_one(path)
        return {
            "loaded": len(self._loaded_modules),
            "errors": len(self._scan_errors),
            "dir":    str(pdir),
        }

    def _load_one(self, path: Path) -> None:
        try:
            spec = importlib.util.spec_from_file_location(
                f"helen_plugin_{path.stem}", path,
            )
            if spec is None or spec.loader is None:
                raise ImportError("no spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            hooks = getattr(module, "HOOKS", None)
            if not isinstance(hooks, dict):
                self._scan_errors.append({
                    "path": str(path), "error": "missing_HOOKS_dict",
                })
                return
            count = 0
            for hook_name, fn in hooks.items():
                if not callable(fn):
                    continue
                with self._lock:
                    self._hooks[str(hook_name)].append((path.stem, fn))
                count += 1
            self._loaded_modules.append({
                "name":   path.stem,
                "path":   str(path),
                "hooks":  count,
            })
            logger.info("plugin_loaded", name=path.stem, hooks=count)
        except Exception as e:
            self._scan_errors.append({
                "path": str(path),
                "error": str(e)[:200],
            })
            logger.warning("plugin_load_failed",
                           path=str(path), error=str(e)[:120])

    # ── Dispatch ──────────────────────────────────────────

    def fire(self, hook_name: str, payload: dict) -> dict:
        """Run every registered handler for ``hook_name``. Each handler
        may mutate the payload (we pass references); a handler that
        returns None signals "drop"."""
        with self._lock:
            handlers = list(self._hooks.get(hook_name, []))
        results: dict[str, dict] = {}
        for plugin_name, fn in handlers:
            try:
                ret = fn(payload)
                if ret is None:
                    results[plugin_name] = {"action": "dropped"}
                    return {"dropped": True, "by": plugin_name,
                            "results": results}
                if isinstance(ret, dict):
                    payload = ret
                    results[plugin_name] = {"action": "transformed"}
                else:
                    results[plugin_name] = {"action": "passthrough"}
            except Exception as e:
                results[plugin_name] = {"action": "error",
                                         "error": str(e)[:120]}
        return {"dropped": False, "payload": payload, "results": results}

    # ── Diagnostics ──────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "plugin_dir":      str(_plugin_dir()),
                "loaded":          list(self._loaded_modules),
                "errors":          list(self._scan_errors),
                "hooks":           {
                    name: [p for p, _ in handlers]
                    for name, handlers in self._hooks.items()
                },
            }


def get_plugins() -> PluginRegistry:
    return PluginRegistry.instance()


def fire(hook_name: str, payload: dict) -> dict:
    return get_plugins().fire(hook_name, payload)
