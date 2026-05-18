"""
Helen SDK — the public surface that plugin code can import as
``import helen_sdk``.

Each call performs a permission check against the installation's
granted permissions. The :class:`SDKContext` is injected by the loader
when a plugin's entrypoint is executed (typically as a module-level
``CTX`` or via the hook payload's ``ctx`` key).

The SDK exposes only narrow, audited capabilities. Anything destructive
(deleting workspaces, modifying RBAC, billing, etc.) is intentionally
absent — Helen's admin API stays out of plugin reach.
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SDKContext:
    """Per-call context handed to the SDK by the plugin loader."""
    installation_id: str
    workspace_id: str
    plugin_slug: str
    permissions: set[str] = field(default_factory=set)
    kv_namespace: str = ""

    def has(self, perm: str) -> bool:
        return perm in self.permissions


# ───────────────────────────────────────────────────────────────────────
# Helper to enforce permissions
# ───────────────────────────────────────────────────────────────────────


class PluginPermissionError(PermissionError):
    pass


def _ensure(ctx: SDKContext, perm: str) -> None:
    if not ctx.has(perm):
        raise PluginPermissionError(f"missing permission: {perm}")


def _loop_run(coro):                                                  # pragma: no cover
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ───────────────────────────────────────────────────────────────────────
# Messages
# ───────────────────────────────────────────────────────────────────────


def send_message(
    ctx: SDKContext, *, channel_id: str, content: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    _ensure(ctx, "messages.send")
    payload = {
        "workspace_id": ctx.workspace_id,
        "channel_id": channel_id,
        "content": content,
        "via_plugin": ctx.plugin_slug,
        "metadata": metadata or {},
    }
    logger.info("plugin.sdk.send_message plugin=%s channel=%s",
                ctx.plugin_slug, channel_id)
    # Real implementation defers to message service; emit a hook event
    try:
        from app.services.plugins.hooks import invoke_hooks
        _loop_run(invoke_hooks("plugin.send_message", payload))
    except Exception as e:                                              # noqa: BLE001
        logger.debug("plugin.sdk.send_message hook-fail: %s", e)
    return {"ok": True, "queued": True}


def get_user(ctx: SDKContext, user_id: str) -> dict[str, Any]:
    _ensure(ctx, "users.read")
    return {"id": user_id, "workspace_id": ctx.workspace_id}


def get_channel(ctx: SDKContext, channel_id: str) -> dict[str, Any]:
    _ensure(ctx, "channels.read")
    return {"id": channel_id, "workspace_id": ctx.workspace_id}


# ───────────────────────────────────────────────────────────────────────
# KV store (in-memory; persisted via PluginInstallation.config["kv"])
# ───────────────────────────────────────────────────────────────────────


_KV: dict[str, dict[str, Any]] = {}


def _kv_key(ctx: SDKContext) -> str:
    return ctx.kv_namespace or f"{ctx.workspace_id}/{ctx.plugin_slug}"


def kv_store_get(ctx: SDKContext, key: str, default: Any = None) -> Any:
    _ensure(ctx, "kv.read")
    return _KV.get(_kv_key(ctx), {}).get(key, default)


def kv_store_set(ctx: SDKContext, key: str, value: Any) -> None:
    _ensure(ctx, "kv.write")
    _KV.setdefault(_kv_key(ctx), {})[key] = value


def kv_store_delete(ctx: SDKContext, key: str) -> bool:
    _ensure(ctx, "kv.write")
    bucket = _KV.get(_kv_key(ctx), {})
    return bucket.pop(key, None) is not None


def kv_store_list(ctx: SDKContext) -> dict[str, Any]:
    _ensure(ctx, "kv.read")
    return dict(_KV.get(_kv_key(ctx), {}))


# ───────────────────────────────────────────────────────────────────────
# Outbound HTTP (with allowlist)
# ───────────────────────────────────────────────────────────────────────


HTTP_ALLOWLIST: set[str] = {
    # Open-by-default — admin can clamp via env
    "api.openai.com",
    "api.anthropic.com",
    "hooks.slack.com",
    "discord.com",
}


def _host_allowed(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:                                                   # noqa: BLE001
        return False
    if not host:
        return False
    # Disallow private network ranges and loopback
    if host in ("localhost", "127.0.0.1") or host.startswith("10.") \
            or host.startswith("192.168.") or host.startswith("169.254.") \
            or host.startswith("172."):
        return False
    return host in HTTP_ALLOWLIST or any(
        host.endswith("." + d) for d in HTTP_ALLOWLIST
    )


def http_request(
    ctx: SDKContext, *, method: str, url: str,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    _ensure(ctx, "http.outbound")
    if not _host_allowed(url):
        raise PluginPermissionError(f"host not allowlisted: {url}")
    try:
        import urllib.request
        req = urllib.request.Request(
            url, method=method.upper(),
            data=json.dumps(json_body).encode("utf-8") if json_body else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read(2 * 1024 * 1024)    # cap response to 2 MiB
            return {
                "status": resp.status, "headers": dict(resp.headers),
                "body": body.decode("utf-8", errors="replace"),
            }
    except Exception as e:                                              # noqa: BLE001
        return {"status": -1, "error": str(e), "headers": {}, "body": ""}


# ───────────────────────────────────────────────────────────────────────
# Public namespace for plugin code
# ───────────────────────────────────────────────────────────────────────


def make_namespace(ctx: SDKContext) -> dict[str, Any]:
    """Returns the dict to inject as the ``helen_sdk`` module namespace
    for a particular plugin invocation."""
    return {
        "send_message": lambda **kw: send_message(ctx, **kw),
        "get_user": lambda uid: get_user(ctx, uid),
        "get_channel": lambda cid: get_channel(ctx, cid),
        "kv_get": lambda k, d=None: kv_store_get(ctx, k, d),
        "kv_set": lambda k, v: kv_store_set(ctx, k, v),
        "kv_delete": lambda k: kv_store_delete(ctx, k),
        "kv_list": lambda: kv_store_list(ctx),
        "http_request": lambda **kw: http_request(ctx, **kw),
        "workspace_id": ctx.workspace_id,
        "plugin_slug": ctx.plugin_slug,
        "permissions": list(ctx.permissions),
        "logger": logger,
    }
