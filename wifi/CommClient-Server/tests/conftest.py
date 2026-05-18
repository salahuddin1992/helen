"""
Pytest configuration and fixtures for CommClient-Server tests.

Provides:
  - event_loop: Session-scoped async event loop
  - db_session: Function-scoped async session with fresh in-memory DB per test
  - client: AsyncClient for HTTP requests with FastAPI app
  - auth_headers: Bearer token for a registered test user
  - second_user_headers: Bearer token for a second test user
  - test_user_data: Test user credentials
"""

from __future__ import annotations

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.main import create_app
from app.core.deps import get_db
from app.core.security import create_access_token, hash_password
from app.models.user import User


# ── Event Loop ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Test Database ──────────────────────────────────────────────


@pytest.fixture
async def test_engine():
    """
    Create a fresh in-memory SQLite async engine for each test function.
    This ensures complete test isolation — no data bleeds between tests.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def db_session(test_engine):
    """Provide a function-scoped async session. Rolls back after each test."""
    async_session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session_factory() as session:
        yield session
        await session.rollback()


# ── FastAPI Client ────────────────────────────────────────────


@pytest.fixture
async def client(db_session):
    """Create FastAPI app with test database and return AsyncClient."""
    app = create_app()

    # Override the get_db dependency to use test session
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Test User Data ────────────────────────────────────────────


@pytest.fixture
def test_user_data():
    """Provide test user credentials."""
    return {
        "username": "testuser",
        "display_name": "Test User",
        "password": "SecurePass123!",
    }


# ── Auth Headers ──────────────────────────────────────────────


@pytest.fixture
async def auth_headers(db_session, test_user_data):
    """Register a test user and return Bearer token headers."""
    username = test_user_data["username"]
    display_name = test_user_data["display_name"]
    password = test_user_data["password"]

    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)

    access_token = create_access_token(user.id)
    await db_session.commit()

    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
async def second_user_headers(db_session):
    """Register a second test user and return Bearer token headers."""
    user = User(
        username="seconduser",
        display_name="Second User",
        password_hash=hash_password("SecurePass456!"),
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)

    access_token = create_access_token(user.id)
    await db_session.commit()

    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
async def admin_headers(db_session):
    """Register a third test user with role=admin and return Bearer token
    headers. Used by tests covering /api/admin/* endpoints which require
    role checks (`require_role('admin')`)."""
    user = User(
        username="adminuser",
        display_name="Admin User",
        password_hash=hash_password("AdminPass789!"),
        status="online",
        role="admin",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)

    # JWT must carry the role claim — `require_role` decodes it from the
    # token rather than re-querying the DB on every request.
    access_token = create_access_token(user.id, role="admin")
    await db_session.commit()

    return {"Authorization": f"Bearer {access_token}"}


# ── Utility Fixtures ──────────────────────────────────────────


@pytest.fixture
async def test_user(db_session, test_user_data):
    """
    Provide the registered test user object.

    Checks if the user already exists (e.g. created by auth_headers fixture)
    before inserting to avoid UNIQUE constraint errors.
    """
    from sqlalchemy import select
    username = test_user_data["username"]

    # Check if user already created (e.g. by auth_headers fixture)
    result = await db_session.execute(
        select(User).where(User.username == username)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    user = User(
        username=username,
        display_name=test_user_data["display_name"],
        password_hash=hash_password(test_user_data["password"]),
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    await db_session.commit()
    return user


@pytest.fixture
async def second_user(db_session):
    """
    Provide a second registered user object.

    Checks if the user already exists (e.g. created by second_user_headers)
    before inserting to avoid UNIQUE constraint errors.
    """
    from sqlalchemy import select
    result = await db_session.execute(
        select(User).where(User.username == "seconduser")
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    user = User(
        username="seconduser",
        display_name="Second User",
        password_hash=hash_password("SecurePass456!"),
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    await db_session.commit()
    return user
