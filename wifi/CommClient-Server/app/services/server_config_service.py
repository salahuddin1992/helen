"""
Runtime server configuration overrides.

Env vars are the source of truth at boot, but a handful of values (currently
just SERVER_NAME) can be changed live via the admin API without restarting
the process. Overrides are persisted as JSON next to the SQLite database so
they survive restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Fields that can be mutated at runtime through the admin API.
_EDITABLE_FIELDS = ("SERVER_NAME",)


class ServerConfigService:
    def __init__(self) -> None:
        self._lock = Lock()

    def _overrides_path(self, settings: Settings | None = None) -> Path:
        s = settings or get_settings()
        sqlite_path = Path(s.SQLITE_PATH)
        if sqlite_path.is_absolute():
            data_dir = sqlite_path.parent
        else:
            data_dir = (s.PROJECT_ROOT / sqlite_path.parent).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "server_overrides.json"

    def _read_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("server_overrides_read_failed", path=str(path), error=str(exc))
            return {}

    def _write_file(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def load_and_apply(self, settings: Settings | None = None) -> dict[str, Any]:
        """Read the overrides file and mutate in-memory settings accordingly."""
        s = settings or get_settings()
        with self._lock:
            overrides = self._read_file(self._overrides_path(s))
            applied: dict[str, Any] = {}
            for field in _EDITABLE_FIELDS:
                if field in overrides and overrides[field] is not None:
                    setattr(s, field, overrides[field])
                    applied[field] = overrides[field]
            if applied:
                logger.info("server_overrides_applied", fields=list(applied.keys()))
            return applied

    def snapshot(self, settings: Settings | None = None) -> dict[str, Any]:
        s = settings or get_settings()
        return {field: getattr(s, field) for field in _EDITABLE_FIELDS}

    def update_server_name(self, name: str) -> dict[str, Any]:
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("server name cannot be empty")
        if len(cleaned) > 64:
            raise ValueError("server name cannot exceed 64 characters")

        s = get_settings()
        with self._lock:
            path = self._overrides_path(s)
            data = self._read_file(path)
            data["SERVER_NAME"] = cleaned
            self._write_file(path, data)
            s.SERVER_NAME = cleaned
        logger.info("server_name_updated", new_name=cleaned)
        return self.snapshot(s)


server_config_service = ServerConfigService()

__all__ = ["ServerConfigService", "server_config_service"]
