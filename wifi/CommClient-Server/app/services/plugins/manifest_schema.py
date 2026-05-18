"""
Plugin manifest schema (v1.0).

Plugins ship a ``plugin.json`` next to their entrypoint that the loader
reads at install time. The :class:`Manifest` model below is the canonical
shape; everything else in :mod:`app.services.plugins` consumes it.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


SCHEMA_VERSION = "1.0"

ALLOWED_PERMISSIONS = (
    "messages.read",
    "messages.send",
    "messages.delete",
    "users.read",
    "channels.read",
    "channels.create",
    "files.read",
    "files.upload",
    "kv.read",
    "kv.write",
    "http.outbound",
    "calls.read",
    "agents.read",
    "agents.invoke",
    "workspace.read",
)

ALLOWED_HOOKS = (
    "on_message_created",
    "on_message_deleted",
    "on_user_joined",
    "on_channel_created",
    "on_file_uploaded",
    "before_send",
    "after_send",
    "on_call_started",
    "on_call_ended",
    "on_agent_event",
)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")
_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$",
)


# ───────────────────────────────────────────────────────────────────────
# Sub-shapes
# ───────────────────────────────────────────────────────────────────────


class UIRoute(BaseModel):
    path: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=128)
    icon: str | None = None
    section: Literal["workspace", "admin", "settings"] = "workspace"


class SettingsSchema(BaseModel):
    """JSONSchema-lite for plugin config fields."""
    fields: list[dict[str, Any]] = Field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────
# Main manifest
# ───────────────────────────────────────────────────────────────────────


class Manifest(BaseModel):
    schema_version: str = Field(SCHEMA_VERSION, alias="schema_version")
    slug: str
    name: str = Field(..., min_length=1, max_length=128)
    version: str
    author: str | None = None
    description: str | None = None
    homepage: HttpUrl | None = None
    min_helen_version: str | None = None
    max_helen_version: str | None = None
    entrypoint: str = Field(..., min_length=1, max_length=256)
    permissions: list[str] = Field(default_factory=list)
    hooks_subscribed: list[str] = Field(default_factory=list)
    ui_routes: list[UIRoute] = Field(default_factory=list)
    settings_schema: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    code_url: HttpUrl | None = None
    code_sha256: str | None = None
    signature: str | None = None
    signed_by: str | None = None

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must be 3-64 chars: a-z 0-9 - _")
        return v

    @field_validator("version", "min_helen_version", "max_helen_version")
    @classmethod
    def _check_ver(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _SEMVER_RE.match(v):
            raise ValueError(f"invalid semver: {v}")
        return v

    @field_validator("permissions")
    @classmethod
    def _check_perms(cls, v: list[str]) -> list[str]:
        for p in v:
            if p not in ALLOWED_PERMISSIONS:
                raise ValueError(f"unknown permission: {p}")
        return v

    @field_validator("hooks_subscribed")
    @classmethod
    def _check_hooks(cls, v: list[str]) -> list[str]:
        for h in v:
            if h not in ALLOWED_HOOKS:
                raise ValueError(f"unknown hook: {h}")
        return v


def parse_manifest(data: dict[str, Any]) -> Manifest:
    """Strict parse with the project-wide validators."""
    return Manifest.model_validate(data)
