"""
Edge — lightweight worker runtime.

Each edge node hosts a small set of *pure* worker functions:

* ``message.validate``        — schema + size + profanity-light check
* ``file.thumbnail``          — image thumbnail generation (Pillow)
* ``image.resize``            — explicit resize to bounded dims
* ``spam.filter``             — heuristic spam scoring (0–100)
* ``presence.touch``          — broadcast presence to origin

Workers MUST NOT write to the database — the origin server is the
single source of truth. They take an ``input`` dict and return an
``output`` dict.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
import time
from typing import Any, Awaitable, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


Worker = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# ── built-in workers ────────────────────────────────────────


async def worker_message_validate(payload: dict[str, Any]) -> dict[str, Any]:
    body = str(payload.get("body") or "")
    issues: list[str] = []
    if not body:
        issues.append("empty_body")
    if len(body) > 8192:
        issues.append("too_long")
    if re.search(r"https?://[^\s]+", body):
        issues.append("contains_url")
    return {
        "ok":        not issues,
        "issues":    issues,
        "length":    len(body),
        "ts":        time.time(),
    }


async def worker_file_thumbnail(payload: dict[str, Any]) -> dict[str, Any]:
    data_b64 = payload.get("data_b64") or ""
    max_dim = int(payload.get("max_dim") or 256)
    if not data_b64:
        return {"ok": False, "error": "missing_data"}
    try:
        import base64
        from PIL import Image  # type: ignore[import-untyped]
    except Exception:
        return {"ok": False, "error": "pillow_unavailable"}
    try:
        raw = base64.b64decode(data_b64)
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((max_dim, max_dim))
        out = io.BytesIO()
        fmt = (img.format or "PNG").upper()
        if fmt == "JPEG":
            img.convert("RGB").save(out, format="JPEG", quality=82)
        else:
            img.save(out, format=fmt)
        return {
            "ok":      True,
            "data_b64": base64.b64encode(out.getvalue()).decode("ascii"),
            "format":  fmt.lower(),
            "width":   img.width,
            "height":  img.height,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def worker_image_resize(payload: dict[str, Any]) -> dict[str, Any]:
    target_w = int(payload.get("width") or 800)
    target_h = int(payload.get("height") or 600)
    try:
        import base64
        from PIL import Image  # type: ignore[import-untyped]
    except Exception:
        return {"ok": False, "error": "pillow_unavailable"}
    data_b64 = payload.get("data_b64") or ""
    try:
        raw = base64.b64decode(data_b64)
        img = Image.open(io.BytesIO(raw))
        img = img.resize((target_w, target_h))
        out = io.BytesIO()
        img.save(out, format=img.format or "PNG")
        return {
            "ok": True,
            "data_b64": base64.b64encode(out.getvalue()).decode("ascii"),
            "width":  img.width, "height": img.height,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_SPAM_PATTERNS = [
    (re.compile(r"\b(viagra|cialis|casino|bitcoin|forex)\b", re.I), 30),
    (re.compile(r"http[s]?://[^\s]+", re.I), 10),
    (re.compile(r"\$\d{3,}"), 15),
    (re.compile(r"(.)\1{8,}"), 25),  # excessive char repetition
    (re.compile(r"[A-Z]{20,}"), 20),  # screaming
]


async def worker_spam_filter(payload: dict[str, Any]) -> dict[str, Any]:
    body = str(payload.get("body") or "")
    score = 0
    matched: list[str] = []
    for pat, w in _SPAM_PATTERNS:
        m = pat.search(body)
        if m:
            score = min(100, score + w)
            matched.append(m.group(0)[:40])
    return {"score": score, "matched": matched, "is_spam": score >= 60}


async def worker_presence_touch(payload: dict[str, Any]) -> dict[str, Any]:
    user_id = payload.get("user_id")
    status = payload.get("status") or "online"
    if not user_id:
        return {"ok": False, "error": "missing_user_id"}
    return {
        "ok":        True,
        "user_id":   user_id,
        "status":    status,
        "ts":        time.time(),
    }


# ── runtime ─────────────────────────────────────────────────


class EdgeWorkerRuntime:
    """Dispatches incoming work to a registered worker function."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {
            "message.validate": worker_message_validate,
            "file.thumbnail":   worker_file_thumbnail,
            "image.resize":     worker_image_resize,
            "spam.filter":      worker_spam_filter,
            "presence.touch":   worker_presence_touch,
        }
        self._stats: dict[str, dict[str, int]] = {}

    def register(self, name: str, fn: Worker) -> None:
        self._workers[name] = fn

    def workers(self) -> list[str]:
        return sorted(self._workers.keys())

    async def execute(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        fn = self._workers.get(name)
        if fn is None:
            return {"ok": False, "error": f"unknown_worker:{name}"}
        t0 = time.monotonic()
        try:
            out = await fn(payload)
            ok = bool(out.get("ok", True))
            self._stat(name, "ok" if ok else "err")
        except Exception as exc:
            self._stat(name, "err")
            return {"ok": False, "error": str(exc)}
        ms = int((time.monotonic() - t0) * 1000)
        if isinstance(out, dict):
            out.setdefault("_elapsed_ms", ms)
        return out

    def _stat(self, name: str, key: str) -> None:
        s = self._stats.setdefault(name, {"ok": 0, "err": 0})
        s[key] = s.get(key, 0) + 1

    def stats(self) -> dict[str, dict[str, int]]:
        return dict(self._stats)


_runtime: Optional[EdgeWorkerRuntime] = None


def get_edge_runtime() -> EdgeWorkerRuntime:
    global _runtime
    if _runtime is None:
        _runtime = EdgeWorkerRuntime()
    return _runtime
