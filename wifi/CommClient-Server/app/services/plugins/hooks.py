"""
Hook registry and dispatcher.

Plugins declare ``hooks_subscribed`` in their manifest; the loader
registers a callable per hook+installation via :func:`register_hook`.

When a Helen subsystem fires an event (e.g. ``on_message_created``), it
calls :func:`invoke_hooks` which dispatches to every subscriber with a
5-second per-call timeout and swallowed exceptions (only logged).
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)


HOOK_TIMEOUT_SEC = 5
HookHandler = Callable[[dict[str, Any]], Any] | Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class _Subscription:
    installation_id: str
    hook: str
    handler: HookHandler


class HookRegistry:
    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscription]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def register(
        self, *, installation_id: str, hook: str, handler: HookHandler,
    ) -> None:
        async with self._lock:
            # Replace any existing subscription for the same installation+hook
            existing = self._subs[hook]
            existing[:] = [s for s in existing if s.installation_id != installation_id]
            existing.append(_Subscription(installation_id, hook, handler))
        logger.info("plugin.hook.registered hook=%s install=%s",
                    hook, installation_id)

    async def unregister_installation(self, installation_id: str) -> int:
        removed = 0
        async with self._lock:
            for hook, subs in list(self._subs.items()):
                before = len(subs)
                subs[:] = [s for s in subs if s.installation_id != installation_id]
                removed += before - len(subs)
        return removed

    async def list_subscribers(self, hook: str) -> list[str]:
        async with self._lock:
            return [s.installation_id for s in self._subs.get(hook, [])]

    async def invoke(self, hook: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Fire every handler subscribed to ``hook``; return per-call summaries."""
        async with self._lock:
            subs = list(self._subs.get(hook, []))
        if not subs:
            return []
        results: list[dict[str, Any]] = []
        for sub in subs:
            t0 = time.perf_counter()
            entry: dict[str, Any] = {
                "installation_id": sub.installation_id,
                "hook": hook,
                "ok": False,
                "duration_ms": 0,
            }
            try:
                fn = sub.handler
                if inspect.iscoroutinefunction(fn):
                    res = await asyncio.wait_for(
                        fn(payload), timeout=HOOK_TIMEOUT_SEC,
                    )
                else:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(fn, payload),
                        timeout=HOOK_TIMEOUT_SEC,
                    )
                entry["ok"] = True
                entry["result"] = res
            except asyncio.TimeoutError:
                entry["error"] = "timeout"
                logger.warning("plugin.hook.timeout %s install=%s",
                               hook, sub.installation_id)
            except Exception as e:                                          # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {e}"
                logger.warning("plugin.hook.error %s install=%s err=%s",
                               hook, sub.installation_id, e)
            finally:
                entry["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            results.append(entry)
            try:
                await _record_event(sub.installation_id, hook, entry)
            except Exception:                                               # noqa: BLE001
                pass
        return results


registry = HookRegistry()


# Convenience aliases
async def register_hook(
    *, installation_id: str, hook: str, handler: HookHandler,
) -> None:
    await registry.register(
        installation_id=installation_id, hook=hook, handler=handler,
    )


async def invoke_hooks(hook: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return await registry.invoke(hook, payload)


async def unregister_installation(installation_id: str) -> int:
    return await registry.unregister_installation(installation_id)


# ───────────────────────────────────────────────────────────────────────
# Audit recording
# ───────────────────────────────────────────────────────────────────────


async def _record_event(
    installation_id: str, hook: str, entry: dict[str, Any],
) -> None:
    """Persist a single hook invocation event (best-effort)."""
    try:
        from app.db.session import async_session_factory
        from app.models.plugin import PluginEvent
        async with async_session_factory() as db:
            db.add(PluginEvent(
                installation_id=installation_id,
                event="hook_called" if entry.get("ok") else "hook_error",
                payload={"hook": hook, **{k: v for k, v in entry.items()
                                          if k not in ("installation_id", "hook")}},
                duration_ms=entry.get("duration_ms", 0),
            ))
            await db.commit()
    except Exception:                                                       # noqa: BLE001
        pass
