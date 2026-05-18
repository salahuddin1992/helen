"""
Shared fixtures for the Helen admin-panels integration test suite.

These fixtures intentionally *do not* depend on the heavyweight global
``app.main:create_app`` boot path. Several router modules pull in optional
runtime dependencies (raw sockets, NATS, redis, native crypto, …) which
are not available in the CI sandbox. Each integration test file is free
to mount the precise subset of routers it needs via the
``_build_admin_app`` factory below.

Fixtures
--------
``tmp_db``
    Fresh in-memory async SQLite engine + session factory per test
    session. Models are created from ``Base.metadata``.

``seed_minimal``
    Bootstraps the default RBAC roles + an ``admin`` user, a regular
    ``user`` account, and a small set of canonical records used by the
    cross-router flow tests (tenant, workspace, license seed, …).

``admin_client``
    ``httpx.AsyncClient`` pre-authenticated with a JWT carrying
    ``role="admin"``.

``user_client``
    ``httpx.AsyncClient`` pre-authenticated with a JWT carrying
    ``role="user"`` (i.e. *not* admin).

``unauth_client``
    Plain ``httpx.AsyncClient`` with no Authorization header.

``admin_app``
    A ``FastAPI`` instance with every one of the 11 admin routers
    mounted. Mounting is best-effort: if a router fails to import in
    the sandbox we log + continue, so smoke tests still run against
    whatever boots successfully.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator, Iterable

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


log = logging.getLogger("helen.tests.integration")


# ─────────────────────────────────────────────────────────────────────
# Router catalogue (single source of truth)
# ─────────────────────────────────────────────────────────────────────

ADMIN_ROUTER_MODULES: tuple[str, ...] = (
    "app.api.routes.admin_monitoring",
    "app.api.routes.admin_topology",
    "app.api.routes.admin_siem",
    "app.api.routes.admin_tenancy_portal",
    "app.api.routes.admin_dr_v2",
    "app.api.routes.admin_plugins",
    "app.api.routes.admin_federation",
    "app.api.routes.admin_qos",
    "app.api.routes.admin_compliance",
    "app.api.routes.admin_onboarding",
    "app.api.routes.admin_router_control",
)


def _build_admin_app(modules: Iterable[str] | None = None) -> tuple[FastAPI, list[str], list[str]]:
    """Mount the 11 admin routers onto a fresh FastAPI app.

    Returns
    -------
    (app, mounted, skipped)
        ``mounted`` and ``skipped`` are lists of router module names so
        that tests can ``pytest.skip`` appropriately when sandbox deps
        are missing.
    """
    app = FastAPI(title="Helen Admin Test App")
    mounted: list[str] = []
    skipped: list[str] = []
    for mod in modules or ADMIN_ROUTER_MODULES:
        try:
            module = __import__(mod, fromlist=["router"])
            app.include_router(getattr(module, "router"))
            mounted.append(mod)
        except Exception as e:  # noqa: BLE001 — sandbox-tolerant
            log.warning("admin_router_skipped %s — %s", mod, e)
            skipped.append(f"{mod}: {e.__class__.__name__}")
    return app, mounted, skipped


# ─────────────────────────────────────────────────────────────────────
# Event loop — session scoped so we can reuse the engine
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def tmp_db():
    """Fresh in-memory SQLite with all SQLAlchemy models registered."""
    from app.db.base import Base
    # Side-effect import to register all models on Base.metadata.
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield engine, sessionmaker
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(tmp_db) -> AsyncIterator[AsyncSession]:
    _engine, sessionmaker = tmp_db
    async with sessionmaker() as session:
        yield session
        await session.rollback()


# ─────────────────────────────────────────────────────────────────────
# Seed minimal data
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def seed_minimal(db_session: AsyncSession) -> dict:
    """Insert the minimum data set required by the cross-router flow tests.

    Returns a dict carrying ids so tests can reference them without
    re-querying.
    """
    from app.core.security import hash_password
    from app.models.user import User

    admin = User(
        username="ops-admin",
        display_name="Ops Admin",
        password_hash=hash_password("AdminPass!2026"),
        status="online",
        role="admin",
    )
    plain = User(
        username="regular-user",
        display_name="Regular User",
        password_hash=hash_password("UserPass!2026"),
        status="online",
        role="user",
    )
    db_session.add_all([admin, plain])
    await db_session.flush()

    # Best-effort: roles registry. Not all sandbox environments will
    # have the rbac models compiled.
    try:
        from app.services.rbac.registry import bootstrap_default_roles
        await bootstrap_default_roles(db_session)
    except Exception as e:  # noqa: BLE001
        log.warning("rbac bootstrap skipped — %s", e)

    await db_session.commit()

    return {
        "admin_id": admin.id,
        "user_id": plain.id,
        "admin_username": admin.username,
        "user_username": plain.username,
    }


# ─────────────────────────────────────────────────────────────────────
# Clients
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def admin_app() -> tuple[FastAPI, list[str], list[str]]:
    """Mount all 11 admin routers onto a fresh FastAPI app."""
    return _build_admin_app()


def _override_db(app: FastAPI, session: AsyncSession) -> None:
    """Wire the request-scoped DB dependency to a single test session."""
    try:
        from app.core.deps import get_db
    except Exception:
        return

    async def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db


@pytest_asyncio.fixture
async def admin_client(admin_app, db_session, seed_minimal) -> AsyncIterator[AsyncClient]:
    """An AsyncClient pre-authenticated as ``admin``."""
    from app.core.security import create_access_token

    app, mounted, skipped = admin_app
    _override_db(app, db_session)

    token = create_access_token(seed_minimal["admin_id"], role="admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        client.helen_meta = {"mounted": mounted, "skipped": skipped, "token": token}  # type: ignore[attr-defined]
        yield client


@pytest_asyncio.fixture
async def user_client(admin_app, db_session, seed_minimal) -> AsyncIterator[AsyncClient]:
    """An AsyncClient pre-authenticated as a non-admin user."""
    from app.core.security import create_access_token

    app, _, _ = admin_app
    _override_db(app, db_session)
    token = create_access_token(seed_minimal["user_id"], role="user")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def unauth_client(admin_app, db_session) -> AsyncIterator[AsyncClient]:
    """An AsyncClient with no Authorization header."""
    app, _, _ = admin_app
    _override_db(app, db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ─────────────────────────────────────────────────────────────────────
# Endpoint discovery helpers (used by the smoke matrix)
# ─────────────────────────────────────────────────────────────────────


def discover_endpoints(app: FastAPI) -> list[dict]:
    """Return a list of route descriptors: {method, path, name, tag, type}."""
    out: list[dict] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        if hasattr(route, "methods") and route.methods:  # HTTP route
            for m in route.methods:
                if m in ("HEAD", "OPTIONS"):
                    continue
                out.append({
                    "method": m,
                    "path": path,
                    "name": getattr(route, "name", ""),
                    "tags": getattr(route, "tags", []) or [],
                    "type": "http",
                })
        elif route.__class__.__name__ == "APIWebSocketRoute":
            out.append({
                "method": "WS",
                "path": path,
                "name": getattr(route, "name", ""),
                "tags": getattr(route, "tags", []) or [],
                "type": "ws",
            })
    return out


# Mark all integration tests as integration (lets users run -m integration).
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: cross-router integration tests")
    config.addinivalue_line("markers", "perf: performance smoke checks")
    config.addinivalue_line("markers", "security: security / authz tests")
    # Ensure JWT secret is deterministic if not already configured
    os.environ.setdefault("JWT_SECRET", "integration-test-secret-do-not-use-in-prod")
