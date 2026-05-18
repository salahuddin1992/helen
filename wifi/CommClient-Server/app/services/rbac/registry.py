"""
Phase 2 / Module G — RBAC permission tree, default-role seed.

The canonical list of permissions and the default-role definitions live
here so the API, the UI, and the migration all agree on shape.

Usage
-----
* Call ``seed_permissions(db)`` once at startup — it INSERTs missing rows
  and never touches existing ones, so safe to call on every boot.
* Call ``bootstrap_default_roles(db)`` to materialise the five system
  roles and wire each to its default permission set. Idempotent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.rbac import Permission, Role, RolePermission

logger = get_logger(__name__)


# ── Permission tree ────────────────────────────────────────

PERMISSION_TREE: dict[str, list[str]] = {
    "messages": ["read", "write", "delete", "edit_others"],
    "channels": ["create", "delete", "archive", "manage_members"],
    "users":    ["read", "kick", "ban", "promote", "reset_password"],
    "system":   ["config_read", "config_write", "backup", "logs", "metrics"],
    "rbac":     ["roles_read", "roles_write", "permissions_assign"],
}


_HUMAN_DESCRIPTIONS: dict[str, str] = {
    "messages.read":            "Read any channel's messages",
    "messages.write":           "Send messages",
    "messages.delete":          "Delete own messages",
    "messages.edit_others":     "Edit or delete other users' messages",
    "channels.create":          "Create new channels",
    "channels.delete":          "Delete channels",
    "channels.archive":         "Archive channels",
    "channels.manage_members":  "Add/remove channel members",
    "users.read":               "View user directory",
    "users.kick":               "Force-disconnect users",
    "users.ban":                "Disable user accounts",
    "users.promote":            "Change user roles (legacy column)",
    "users.reset_password":     "Force password reset on a user",
    "system.config_read":       "View server configuration",
    "system.config_write":      "Modify server configuration",
    "system.backup":            "Create / restore database backups",
    "system.logs":              "Read live logs and crash reports",
    "system.metrics":           "Access metrics dashboard and exports",
    "rbac.roles_read":          "View roles and permission assignments",
    "rbac.roles_write":         "Create / edit / delete roles",
    "rbac.permissions_assign":  "Attach permissions to roles, assign roles to users",
}


# ── Default role definitions ───────────────────────────────

DEFAULT_ROLES: list[dict[str, Any]] = [
    {
        "name": "superadmin",
        "description": "Unrestricted access. Cannot be deleted.",
        "is_system": True,
        # superadmin always resolves to "all permissions" at lookup time.
        "permissions": "*",
    },
    {
        "name": "admin",
        "description": "Server administrator (everything except RBAC self-modification).",
        "is_system": True,
        "permissions": [
            "messages.read", "messages.write", "messages.delete", "messages.edit_others",
            "channels.create", "channels.delete", "channels.archive", "channels.manage_members",
            "users.read", "users.kick", "users.ban", "users.promote", "users.reset_password",
            "system.config_read", "system.config_write",
            "system.backup", "system.logs", "system.metrics",
            "rbac.roles_read",
        ],
    },
    {
        "name": "moderator",
        "description": "Channel + user moderation, no system controls.",
        "is_system": True,
        "permissions": [
            "messages.read", "messages.write", "messages.delete", "messages.edit_others",
            "channels.archive", "channels.manage_members",
            "users.read", "users.kick",
            "system.logs",
        ],
    },
    {
        "name": "member",
        "description": "Normal authenticated user.",
        "is_system": True,
        "permissions": [
            "messages.read", "messages.write", "messages.delete",
            "channels.create",
            "users.read",
        ],
    },
    {
        "name": "guest",
        "description": "Read-only visitor.",
        "is_system": True,
        "permissions": [
            "messages.read",
            "users.read",
        ],
    },
]


def all_permission_keys() -> list[str]:
    return [f"{cat}.{verb}" for cat, verbs in PERMISSION_TREE.items() for verb in verbs]


def is_valid_permission(key: str) -> bool:
    return key in set(all_permission_keys())


# ── DB seeding ─────────────────────────────────────────────

async def seed_permissions(db: AsyncSession) -> int:
    """Insert any missing permission rows. Returns inserted count."""
    existing = {
        row[0] for row in (await db.execute(select(Permission.key))).all()
    }
    inserted = 0
    for cat, verbs in PERMISSION_TREE.items():
        for verb in verbs:
            key = f"{cat}.{verb}"
            if key in existing:
                continue
            db.add(Permission(
                key=key,
                category=cat,
                description=_HUMAN_DESCRIPTIONS.get(key, key),
            ))
            inserted += 1
    if inserted:
        await db.flush()
    return inserted


async def bootstrap_default_roles(db: AsyncSession) -> dict[str, str]:
    """Materialise system roles + their permission mappings. Idempotent.

    Returns a ``{role_name: role_id}`` map for callers that need it."""
    await seed_permissions(db)

    perms_by_key: dict[str, Permission] = {
        p.key: p for p in (await db.execute(select(Permission))).scalars().all()
    }
    role_by_name: dict[str, Role] = {
        r.name: r for r in (await db.execute(select(Role))).scalars().all()
    }

    out: dict[str, str] = {}
    for spec in DEFAULT_ROLES:
        name = spec["name"]
        role = role_by_name.get(name)
        if role is None:
            role = Role(
                name=name,
                description=spec.get("description"),
                is_system=bool(spec.get("is_system", False)),
            )
            db.add(role)
            await db.flush()
            role_by_name[name] = role

        # Wire permissions
        if spec["permissions"] == "*":
            wanted_keys = set(perms_by_key.keys())
        else:
            wanted_keys = set(spec["permissions"])

        existing_perm_ids = {
            rp.permission_id
            for rp in (await db.execute(
                select(RolePermission).where(RolePermission.role_id == role.id)
            )).scalars().all()
        }
        for key in wanted_keys:
            perm = perms_by_key.get(key)
            if perm is None:
                continue
            if perm.id in existing_perm_ids:
                continue
            db.add(RolePermission(role_id=role.id, permission_id=perm.id, granted=True))

        out[name] = role.id

    await db.flush()
    return out


# Sentinel — the superadmin shortcut, used by the enforcer.
SUPERADMIN_ROLE_NAME = "superadmin"
