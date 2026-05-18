"""
Phase 3 / Module M — ORM-level tenant isolation helpers.

Two flavors:

1. ``apply_tenant_filter(query, workspace_id, model=None)`` — append a
   ``WHERE workspace_id = :wid`` clause to a SQLAlchemy ``select()``.
   The ``model`` parameter is auto-detected from the primary FROM-entity
   when omitted. Models that don't yet have a ``workspace_id`` column
   are passed through untouched so we can migrate gradually.

2. ``@workspace_scoped`` — endpoint decorator that pulls ``workspace``
   from kwargs (injected by the FastAPI dep) and stashes
   ``workspace_id`` on ``contextvars`` so deeper helpers see it too.

We also export a ``CurrentWorkspace`` ContextVar — set by the
middleware (or the decorator) and read by anything that needs the
ambient tenant without threading it through every signature.
"""
from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import Any, Callable, Optional, TypeVar

from sqlalchemy import inspect
from sqlalchemy.sql import Select

from app.core.logging import get_logger
from app.models.workspace import Workspace

logger = get_logger(__name__)

T = TypeVar("T")

# ── ContextVar — ambient tenant ─────────────────────────────
CurrentWorkspace: ContextVar[Optional[str]] = ContextVar(
    "CurrentWorkspace", default=None,
)


def set_current_workspace(workspace_id: Optional[str]) -> None:
    CurrentWorkspace.set(workspace_id)


def get_current_workspace() -> Optional[str]:
    return CurrentWorkspace.get()


# ── Query helper ────────────────────────────────────────────

def _primary_entity(stmt: Select) -> Optional[type]:
    """Resolve the leftmost ORM entity in ``stmt``. Best-effort."""
    try:
        froms = list(stmt.get_final_froms())
        if not froms:
            return None
        target = froms[0]
        if hasattr(target, "entity_namespace"):
            return target.entity_namespace
        mapper = inspect(target, raiseerr=False)
        if mapper is not None and hasattr(mapper, "class_"):
            return mapper.class_
    except Exception:                                          # pragma: no cover
        return None
    return None


def model_supports_tenant(model: Any) -> bool:
    """``True`` iff the model has a ``workspace_id`` mapped column."""
    if model is None:
        return False
    try:
        mapper = inspect(model)
        return "workspace_id" in mapper.columns
    except Exception:
        return False


def apply_tenant_filter(
    stmt: Select,
    workspace_id: Optional[str],
    model: Optional[Any] = None,
) -> Select:
    """Append a ``workspace_id = :wid`` filter. Pass-through if either the
    model lacks a workspace column or no tenant is in context."""
    if not workspace_id:
        return stmt
    target = model or _primary_entity(stmt)
    if not model_supports_tenant(target):
        return stmt
    return stmt.where(getattr(target, "workspace_id") == workspace_id)


# ── Decorator ───────────────────────────────────────────────

def workspace_scoped(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator for endpoint handlers that depend on
    ``current_workspace_dependency``. It snapshots the resolved
    Workspace into the ``CurrentWorkspace`` ContextVar so that nested
    service calls can pick it up without explicit threading."""

    @functools.wraps(func)
    async def _async_wrapper(*args, **kwargs):
        ws: Optional[Workspace] = kwargs.get("workspace") or kwargs.get("current_workspace")
        token = CurrentWorkspace.set(ws.id if ws else None)
        try:
            return await func(*args, **kwargs)
        finally:
            CurrentWorkspace.reset(token)

    @functools.wraps(func)
    def _sync_wrapper(*args, **kwargs):
        ws: Optional[Workspace] = kwargs.get("workspace") or kwargs.get("current_workspace")
        token = CurrentWorkspace.set(ws.id if ws else None)
        try:
            return func(*args, **kwargs)
        finally:
            CurrentWorkspace.reset(token)

    import asyncio
    if asyncio.iscoroutinefunction(func):
        return _async_wrapper          # type: ignore[return-value]
    return _sync_wrapper                # type: ignore[return-value]
