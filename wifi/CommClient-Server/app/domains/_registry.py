"""
app.domains._registry — central registry for domain introspection.

Used by the admin UI ("Domain Topology" panel) and by ops tooling to
verify which optional modules are present in a given deployment.

The registry is built LAZILY: importing this module does not trigger
any domain imports; calling ``build_registry()`` walks each domain
package and records what was actually re-exported.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from app.domains import DOMAIN_NAMES, DOMAINS_VERSION

log = logging.getLogger(__name__)

# Cache so repeated admin-UI hits don't re-walk every package.
_CACHE: dict[str, dict[str, Any]] | None = None


def _inspect_domain(name: str) -> dict[str, Any]:
    """Return ``{symbols, count, error}`` for a single domain."""
    qualname = f"app.domains.{name}"
    try:
        mod = importlib.import_module(qualname)
    except Exception as exc:
        log.warning("domain %s failed to import: %s", qualname, exc)
        return {"symbols": [], "count": 0, "error": str(exc), "loaded": False}

    syms: list[str] = list(getattr(mod, "__all__", []) or [])
    return {
        "symbols": sorted(syms),
        "count": len(syms),
        "error": None,
        "loaded": True,
    }


def build_registry(force: bool = False) -> dict[str, dict[str, Any]]:
    """Walk every domain package and snapshot its re-exported surface.

    Returns a mapping ``{domain_name: {symbols, count, error, loaded}}``.
    Cached after first call; pass ``force=True`` to rebuild.
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    out: dict[str, dict[str, Any]] = {}
    for name in DOMAIN_NAMES:
        out[name] = _inspect_domain(name)
    _CACHE = out
    return out


def get_summary() -> dict[str, Any]:
    """One-shot summary for admin-UI cards / Prometheus exposition."""
    reg = build_registry()
    total_symbols = sum(d["count"] for d in reg.values())
    loaded_domains = sum(1 for d in reg.values() if d["loaded"])
    return {
        "version": DOMAINS_VERSION,
        "domains_total": len(DOMAIN_NAMES),
        "domains_loaded": loaded_domains,
        "symbols_total": total_symbols,
        "details": reg,
    }


def find_symbol(name: str) -> list[str]:
    """Reverse-lookup: which domains export the given symbol?"""
    reg = build_registry()
    hits: list[str] = []
    for dom, info in reg.items():
        if name in info["symbols"]:
            hits.append(dom)
    return hits


__all__ = [
    "build_registry",
    "get_summary",
    "find_symbol",
]
