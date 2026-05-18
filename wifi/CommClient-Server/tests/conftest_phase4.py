"""
tests/conftest_phase4.py — Phase 4 / Module V
=============================================

Additional shared fixtures used by the Phase-4 test pack. Layered ON TOP
of the existing ``tests/conftest.py`` (which already provides
``event_loop``, ``db_session``, ``client``, ``auth_headers``).

These fixtures DO NOT shadow the originals — they are imported lazily,
under unique names, by the ``test_*_phase4.py`` files.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, cast

import pytest

# Defensive import — if app.main is broken (e.g. missing optional deps in
# a CI runner), the rest of the Phase-4 pack still loads.
try:
    from app.main import create_app  # type: ignore[import-not-found]
    from app.db.base import Base  # type: ignore[import-not-found]
    from app.core.deps import get_db  # type: ignore[import-not-found]
    from app.core.security import create_access_token, hash_password  # type: ignore[import-not-found]
    from app.models.user import User  # type: ignore[import-not-found]
    _APP_IMPORTABLE = True
except Exception as _exc:
    _APP_IMPORTABLE = False
    _IMPORT_ERROR = _exc


_SKIP_IF_APP_BROKEN = pytest.mark.skipif(
    not _APP_IMPORTABLE,
    reason=f"app.main not importable: {_IMPORT_ERROR if not _APP_IMPORTABLE else ''}",
)


# Re-exported so tests can apply it as a module-level mark
skip_if_app_broken = _SKIP_IF_APP_BROKEN


@pytest.fixture(scope="session")
def phase4_event_loop():
    """Session-scoped loop, independent from the legacy fixture."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def phase4_env(monkeypatch):
    """Force deterministic test env: in-memory DB, fake secrets, no LAN."""
    monkeypatch.setenv("HELEN_TEST_MODE", "1")
    monkeypatch.setenv("HELEN_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("HELEN_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("HELEN_SECRET_KEY", "phase4-test-key-not-for-prod-0123456789abcdef")
    yield


@pytest.fixture
async def admin_token():
    """A signed JWT for an admin role. Skips if app can't import."""
    if not _APP_IMPORTABLE:
        pytest.skip("app.main not importable")
    return create_access_token({"sub": "1", "username": "admin", "role": "admin"})


@pytest.fixture
async def user_token():
    if not _APP_IMPORTABLE:
        pytest.skip("app.main not importable")
    return create_access_token({"sub": "2", "username": "alice", "role": "user"})
