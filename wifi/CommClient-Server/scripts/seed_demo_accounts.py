"""
Seed 10 admin + 10 client accounts with simple matching passwords.

Usernames and passwords are intentionally weak (`admin1`/`admin1`,
`user1`/`user1`, …) — these accounts are for local-LAN development and
demos. Bypasses the API password-strength validator by inserting
directly into the DB with a bcrypt-hashed password.

Run:
    python scripts/seed_demo_accounts.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add the project root so `app.*` imports resolve.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bcrypt
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.user import User
from app.core.share_code import generate_share_code

ADMINS = [(f"admin{i}", f"admin{i}",  f"Admin {i}")  for i in range(1, 11)]
USERS  = [(f"user{i}",  f"user{i}",   f"User {i}")   for i in range(1, 11)]


def _hash(password: str) -> str:
    # cost 4 — fast for demo seed; production uses 12 via core.security.hash_password
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")


async def seed() -> None:
    async with async_session_factory() as session:
        async with session.begin():
            existing_q = await session.execute(
                select(User.username).where(User.username.in_(
                    [u for u, _, _ in ADMINS + USERS]
                ))
            )
            existing = {row[0] for row in existing_q.all()}

            created_admins: list[tuple[str, str]] = []
            created_users:  list[tuple[str, str]] = []
            skipped: list[str] = []

            for username, password, display in ADMINS:
                if username in existing:
                    skipped.append(username)
                    continue
                session.add(User(
                    username=username,
                    display_name=display,
                    password_hash=_hash(password),
                    share_code=generate_share_code(),
                    role="admin",
                ))
                created_admins.append((username, password))

            for username, password, display in USERS:
                if username in existing:
                    skipped.append(username)
                    continue
                session.add(User(
                    username=username,
                    display_name=display,
                    password_hash=_hash(password),
                    share_code=generate_share_code(),
                    role="user",
                ))
                created_users.append((username, password))

    print()
    print("=" * 60)
    print(" DEMO ACCOUNTS — local LAN development only ".center(60, "="))
    print("=" * 60)
    print()
    if created_admins:
        print(" ADMINS ".center(60, "-"))
        print(f"  {'username':<12} {'password':<12} role")
        for u, p in created_admins:
            print(f"  {u:<12} {p:<12} admin")
        print()
    if created_users:
        print(" CLIENTS ".center(60, "-"))
        print(f"  {'username':<12} {'password':<12} role")
        for u, p in created_users:
            print(f"  {u:<12} {p:<12} user")
        print()
    if skipped:
        print(" SKIPPED (already exist) ".center(60, "-"))
        for u in skipped:
            print(f"  {u}")
        print()
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(seed())
