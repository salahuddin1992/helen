"""Shared safe-import helper for domain facades.

Every domain calls ``safe_import("modpath", ["sym1", "sym2"])`` and
merges the returned dict into ``globals()``. Missing modules / symbols
are silently skipped with a DEBUG log line so that optional Phase-2/3
modules can be omitted from minimal deployments without breaking
``import app.domains.auth``.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("app.domains")


def safe_import(modpath: str, names: list[str]) -> dict[str, Any]:
    """Best-effort import: return a dict of ``{name: attr}`` pairs found.

    Never raises. Missing module = empty dict. Missing attribute = skipped.
    A DEBUG log line records every skip so operators can audit the facade
    after a deploy.
    """
    out: dict[str, Any] = {}
    try:
        mod = __import__(modpath, fromlist=list(names))
    except Exception as exc:
        log.debug("[domains] module %s skipped: %s", modpath, exc)
        return out
    for n in names:
        if hasattr(mod, n):
            out[n] = getattr(mod, n)
        else:
            log.debug("[domains] %s lacks attribute %s", modpath, n)
    return out


def safe_module(modpath: str) -> Any | None:
    """Return the imported module or None — used when a domain wants the
    whole namespace under a single attribute name (e.g. ``socket_handlers``)."""
    try:
        return __import__(modpath, fromlist=["*"])
    except Exception as exc:
        log.debug("[domains] module %s skipped: %s", modpath, exc)
        return None


__all__ = ["safe_import", "safe_module"]
