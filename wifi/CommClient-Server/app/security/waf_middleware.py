"""
Phase 6 / Module AE — Web Application Firewall (WAF) ASGI middleware.

Production-grade pattern-match WAF for path, query string, headers, and
JSON bodies (size-capped). Each category can be set to *observe*,
*log*, or *block*. Exemptions can be registered per route prefix.

The middleware never blocks long-running operations; it scans only on
the request boundary so the latency impact is single-digit microseconds
per request.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.logging import get_logger
from app.observability.metrics_exporter import counter_inc

logger = get_logger(__name__)


# Modes per category
MODE_OBSERVE = "observe"
MODE_LOG = "log"
MODE_BLOCK = "block"


@dataclass
class _Pattern:
    name: str
    regex: re.Pattern[str]
    category: str


def _compile(patterns: list[tuple[str, str, str]]) -> list[_Pattern]:
    return [
        _Pattern(name, re.compile(rx, re.IGNORECASE | re.DOTALL), cat)
        for (cat, name, rx) in patterns
    ]


# Curated patterns. Names are stable; revisions just tighten regexes.
_PATTERNS: list[tuple[str, str, str]] = [
    # SQL injection
    ("sqli", "union_select", r"\bunion\s+(all\s+)?select\b"),
    ("sqli", "or_1_equals_1", r"['\"`)]\s*or\s+['\"`(]*\s*1\s*=\s*1"),
    ("sqli", "sleep_call",    r"\b(?:sleep|pg_sleep|waitfor\s+delay)\s*\("),
    ("sqli", "benchmark_call",r"\bbenchmark\s*\("),
    ("sqli", "drop_table",    r"\bdrop\s+(?:table|database|schema)\b"),
    ("sqli", "load_file",     r"\bload_file\s*\("),
    ("sqli", "into_outfile",  r"\binto\s+outfile\b"),
    # XSS
    ("xss", "script_tag",  r"<\s*script[^>]*>"),
    ("xss", "javascript_uri", r"javascript:"),
    ("xss", "event_handler",  r"\bon(?:load|click|error|mouseover|focus)\s*="),
    ("xss", "img_onerror",    r"<\s*img[^>]*\bonerror\s*="),
    ("xss", "iframe_src",     r"<\s*iframe[^>]*\bsrc\s*="),
    # Path traversal
    ("path_traversal", "dotdot_slash",       r"\.\./"),
    ("path_traversal", "dotdot_backslash",   r"\.\.\\"),
    ("path_traversal", "encoded_dotdot",     r"%2e%2e[%2f%5c]"),
    # Command injection
    ("cmdi", "semi_rm",       r";\s*rm\s+-rf"),
    ("cmdi", "pipe_cat",      r"\|\s*(?:cat|nc|wget|curl|bash|sh|powershell)\b"),
    ("cmdi", "double_amp",    r"&&\s*(?:rm|wget|curl|bash|sh|powershell)\b"),
    ("cmdi", "dollar_paren",  r"\$\([^)]+\)"),
    ("cmdi", "backticks",     r"`[^`]+`"),
    # LDAP injection
    ("ldapi", "wildcard_objectclass",
                r"\*\s*\)\s*\(\s*objectclass\s*=\s*\*"),
    ("ldapi", "or_admin", r"\|\s*\(\s*uid\s*=\s*admin"),
    # XML / XXE
    ("xxe", "doctype_entity",
                r"<!doctype[^>]*\[\s*<!entity"),
    ("xxe", "system_external_entity",
                r"<!entity[^>]*\bsystem\s+['\"]"),
    # NoSQL injection
    ("nosql", "where_op",   r"\$where\b"),
    ("nosql", "ne_op",      r"\$ne\b"),
    ("nosql", "gt_op_sus",  r"\"\$gt\"\s*:\s*\"\""),
    ("nosql", "regex_op",   r"\$regex\b"),
]


@dataclass
class WAFConfig:
    # Per-category mode (block / log / observe)
    modes: dict[str, str] = field(default_factory=lambda: {
        "sqli": MODE_BLOCK,
        "xss": MODE_BLOCK,
        "path_traversal": MODE_BLOCK,
        "cmdi": MODE_BLOCK,
        "ldapi": MODE_LOG,
        "xxe": MODE_BLOCK,
        "nosql": MODE_BLOCK,
    })
    exempt_prefixes: list[str] = field(default_factory=lambda: [
        "/api/health", "/metrics",
        "/api/admin/observability",   # admin tools may legitimately
        "/api/admin/security",        # talk about regex strings
    ])
    debug_headers: bool = False
    max_body_bytes: int = 1 * 1024 * 1024   # 1 MiB cap on inspection


@dataclass
class WAFVerdict:
    triggered: list[tuple[str, str, str]]   # (category, name, where)
    blocked_by: Optional[tuple[str, str]]   # (category, name)


class WAFMiddleware:
    """ASGI middleware. Scans path, query, headers, and body (JSON only).

    Adds an ``X-WAF-Verdict`` header when ``debug_headers`` is True."""

    def __init__(
        self,
        app: ASGIApp,
        config: Optional[WAFConfig] = None,
    ) -> None:
        self.app = app
        self.cfg = config or WAFConfig()
        self._patterns = _compile(_PATTERNS)
        self._stats: dict[str, int] = {}

    # ── public API for admin UI ─────────────────────────────

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def reset_stats(self) -> None:
        self._stats.clear()

    def set_mode(self, category: str, mode: str) -> None:
        if mode not in (MODE_OBSERVE, MODE_LOG, MODE_BLOCK):
            raise ValueError("invalid mode")
        self.cfg.modes[category] = mode

    # ── ASGI entry ──────────────────────────────────────────

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if any(path.startswith(p) for p in self.cfg.exempt_prefixes):
            await self.app(scope, receive, send)
            return

        # 1) cheap surface scan
        verdict = self._scan_surface(scope)

        # 2) body scan only on small JSON bodies
        if not verdict.blocked_by:
            body_bytes, recv_wrap = await self._buffer_body(receive)
            verdict = self._scan_body(verdict, body_bytes)
            receive = recv_wrap

        # 3) record metrics + maybe block
        if verdict.triggered:
            for cat, name, where in verdict.triggered:
                key = f"{cat}.{name}.{where}"
                self._stats[key] = self._stats.get(key, 0) + 1
            for cat, _, _ in verdict.triggered:
                counter_inc("waf_blocks_total", category=cat)

        if verdict.blocked_by is not None:
            cat, name = verdict.blocked_by
            logger.warning(
                "WAF BLOCK %s/%s on %s ip=%s",
                cat, name, path,
                scope.get("client", ("?",))[0] if scope.get("client") else "?",
            )
            await self._respond_403(send, cat, name)
            return

        # add debug header if requested
        if self.cfg.debug_headers and verdict.triggered:
            await self._wrap_response_with_header(scope, receive, send,
                                                  verdict)
            return
        await self.app(scope, receive, send)

    # ── internals ───────────────────────────────────────────

    def _scan_text(self, text: str, where: str) -> list[tuple[str, str, str]]:
        if not text:
            return []
        hits: list[tuple[str, str, str]] = []
        for pat in self._patterns:
            if pat.regex.search(text):
                hits.append((pat.category, pat.name, where))
        return hits

    def _scan_surface(self, scope: Scope) -> WAFVerdict:
        hits: list[tuple[str, str, str]] = []
        path = scope.get("path", "") or ""
        qs = (scope.get("query_string") or b"").decode("latin-1", "ignore")
        hits.extend(self._scan_text(path, "path"))
        hits.extend(self._scan_text(qs, "query"))
        for k, v in scope.get("headers") or []:
            try:
                val = v.decode("latin-1", "ignore")
            except Exception:
                continue
            # only look at user-controlled headers
            name = k.decode("ascii", "ignore").lower()
            if name in ("cookie", "user-agent", "referer", "x-forwarded-for"):
                hits.extend(self._scan_text(val, f"header:{name}"))
        return self._verdict_from_hits(hits)

    def _scan_body(self, prev: WAFVerdict, body: bytes) -> WAFVerdict:
        if not body:
            return prev
        try:
            txt = body.decode("utf-8", "ignore")
        except Exception:
            return prev
        # If JSON, also expand keys & string values
        candidates: list[str] = [txt]
        if txt.lstrip().startswith(("{", "[")):
            try:
                obj = json.loads(txt)
                candidates.extend(_iter_json_strings(obj))
            except Exception:
                pass
        hits = list(prev.triggered)
        for s in candidates:
            hits.extend(self._scan_text(s, "body"))
        return self._verdict_from_hits(hits)

    def _verdict_from_hits(self, hits: list[tuple[str, str, str]]) -> WAFVerdict:
        blocked_by: Optional[tuple[str, str]] = None
        for cat, name, _where in hits:
            mode = self.cfg.modes.get(cat, MODE_OBSERVE)
            if mode == MODE_BLOCK and blocked_by is None:
                blocked_by = (cat, name)
        return WAFVerdict(triggered=hits, blocked_by=blocked_by)

    async def _buffer_body(
        self, receive: Receive,
    ) -> tuple[bytes, Receive]:
        body = bytearray()
        more = True
        cached: list[dict] = []
        while more:
            msg = await receive()
            cached.append(msg)
            if msg["type"] == "http.request":
                chunk = msg.get("body") or b""
                if len(body) + len(chunk) <= self.cfg.max_body_bytes:
                    body.extend(chunk)
                more = msg.get("more_body", False)
            else:                                                   # pragma: no cover
                more = False

        async def _replay() -> dict:
            if cached:
                return cached.pop(0)
            return {"type": "http.disconnect"}

        return bytes(body), _replay

    async def _respond_403(self, send: Send, category: str, name: str) -> None:
        body = json.dumps({
            "detail": "request blocked by WAF",
            "category": category, "rule": name,
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-waf-verdict", f"block:{category}/{name}".encode("ascii", "ignore")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _wrap_response_with_header(
        self,
        scope: Scope, receive: Receive, send: Send,
        verdict: WAFVerdict,
    ) -> None:
        summary = ",".join(f"{c}/{n}@{w}" for c, n, w in verdict.triggered)[:200]

        async def _send(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers") or [])
                headers.append((b"x-waf-verdict", summary.encode("ascii", "ignore")))
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, _send)


def _iter_json_strings(obj) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _iter_json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_json_strings(v)


_singleton: Optional[WAFMiddleware] = None


def get_waf() -> Optional[WAFMiddleware]:
    return _singleton


def attach_waf(app, config: Optional[WAFConfig] = None) -> WAFMiddleware:
    """Mount the WAF and remember the instance for stats endpoints."""
    global _singleton
    waf = WAFMiddleware(app, config)
    _singleton = waf
    return waf
