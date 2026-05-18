"""
SIEM Alert Rules engine.

Operators define small detection rules in a tiny DSL that fires on
audit chain entries in real time (via the chain pub-sub hook) or in
batch dry-runs.

DSL grammar (informal)
----------------------
    rule        := expr
    expr        := term (("AND"|"OR") term)*
    term        := "NOT" term | "(" expr ")" | predicate
    predicate   := field op value
                 | "WITHIN" INT "s" "OF" predicate     (rate threshold)
    field       := actor | action | resource | severity | <payload.*>
    op          := "=" | "!=" | ">=" | "<=" | ">" | "<" | "IN" | "NOT IN"
    value       := STRING | NUMBER | "[" value ("," value)* "]"

Examples
--------
    actor = "admin1" AND action = "channel.delete"
    severity >= "high" AND NOT actor = "system"
    action IN ["auth.login", "auth.logout"] AND payload.success = false
    WITHIN 60s OF (action = "auth.login" AND payload.success = false)  // rate

Severities are ranked info < low < medium < high < critical.

Thread safety
-------------
The engine maintains an in-memory rule cache (DB-loaded) and a sliding
window for WITHIN rules. Updates after CRUD operations must call
``reload()`` to refresh the cache.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from app.core.logging import get_logger
from app.services.audit.chain import AuditEntry

logger = get_logger(__name__)

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


# ── Tokeniser ────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"""
    \s+                                |
    (?P<STR>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')   |
    (?P<NUM>-?\d+(?:\.\d+)?)           |
    (?P<OP>>=|<=|!=|=|>|<)             |
    (?P<LB>\[)                         |
    (?P<RB>\])                         |
    (?P<LP>\()                         |
    (?P<RP>\))                         |
    (?P<COMMA>,)                       |
    (?P<IDENT>[A-Za-z_][A-Za-z0-9_\.]*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"AND", "OR", "NOT", "IN", "WITHIN", "OF", "TRUE", "FALSE", "NULL", "S"}


class DSLError(ValueError):
    """Raised on malformed DSL expressions."""


def _tokenise(src: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    while i < len(src):
        m = _TOKEN_RE.match(src, i)
        if not m:
            raise DSLError(f"unexpected char at {i}: {src[i:i+20]!r}")
        i = m.end()
        if m.lastgroup is None:
            continue  # whitespace
        kind = m.lastgroup
        val = m.group()
        if kind == "IDENT" and val.upper() in _KEYWORDS:
            tokens.append((val.upper(), val.upper()))
        elif kind == "STR":
            tokens.append(("STR", val[1:-1]))
        elif kind == "NUM":
            tokens.append(("NUM", val))
        else:
            tokens.append((kind, val))
    return tokens


# ── AST nodes ────────────────────────────────────────────────────────────


@dataclass
class _And:
    left: Any
    right: Any


@dataclass
class _Or:
    left: Any
    right: Any


@dataclass
class _Not:
    inner: Any


@dataclass
class _Predicate:
    field: str
    op: str
    value: Any


@dataclass
class _Within:
    seconds: int
    inner: Any
    threshold: int = 2  # default: 2 matches within window


# ── Parser ───────────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self, k: int = 0) -> Optional[tuple[str, str]]:
        if self.pos + k < len(self.tokens):
            return self.tokens[self.pos + k]
        return None

    def eat(self, kind: str) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or tok[0] != kind:
            raise DSLError(f"expected {kind} got {tok}")
        self.pos += 1
        return tok

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise DSLError(f"trailing tokens at pos={self.pos}: {self.peek()}")
        return node

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.peek() and self.peek()[0] == "OR":
            self.pos += 1
            right = self.parse_and()
            left = _Or(left, right)
        return left

    def parse_and(self) -> Any:
        left = self.parse_term()
        while self.peek() and self.peek()[0] == "AND":
            self.pos += 1
            right = self.parse_term()
            left = _And(left, right)
        return left

    def parse_term(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise DSLError("unexpected end of expression")
        if tok[0] == "NOT":
            self.pos += 1
            return _Not(self.parse_term())
        if tok[0] == "LP":
            self.pos += 1
            inner = self.parse_or()
            self.eat("RP")
            return inner
        if tok[0] == "WITHIN":
            self.pos += 1
            n = self.eat("NUM")
            secs = int(float(n[1]))
            nxt = self.peek()
            if nxt and nxt[0] == "IDENT" and nxt[1].lower() == "s":
                self.pos += 1
            if self.peek() and self.peek()[0] == "OF":
                self.pos += 1
            inner = self.parse_term()
            return _Within(seconds=secs, inner=inner)
        return self.parse_predicate()

    def parse_predicate(self) -> _Predicate:
        ident = self.eat("IDENT")
        field_name = ident[1]
        op_tok = self.peek()
        if op_tok and op_tok[0] == "OP":
            self.pos += 1
            op = op_tok[1]
            value = self.parse_value()
            return _Predicate(field_name, op, value)
        if op_tok and op_tok[0] == "IN":
            self.pos += 1
            self.eat("LB")
            values = self.parse_value_list()
            self.eat("RB")
            return _Predicate(field_name, "IN", values)
        if op_tok and op_tok[0] == "NOT":
            self.pos += 1
            nxt = self.peek()
            if nxt and nxt[0] == "IN":
                self.pos += 1
                self.eat("LB")
                values = self.parse_value_list()
                self.eat("RB")
                return _Predicate(field_name, "NOT IN", values)
            raise DSLError("expected IN after NOT")
        raise DSLError(f"expected operator after {field_name}")

    def parse_value(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise DSLError("expected value")
        if tok[0] == "STR":
            self.pos += 1
            return tok[1]
        if tok[0] == "NUM":
            self.pos += 1
            return float(tok[1]) if "." in tok[1] else int(tok[1])
        if tok[0] in ("TRUE", "FALSE"):
            self.pos += 1
            return tok[0] == "TRUE"
        if tok[0] == "NULL":
            self.pos += 1
            return None
        if tok[0] == "IDENT":
            self.pos += 1
            return tok[1]
        if tok[0] == "LB":
            self.pos += 1
            values = self.parse_value_list()
            self.eat("RB")
            return values
        raise DSLError(f"unexpected value token {tok}")

    def parse_value_list(self) -> list[Any]:
        values: list[Any] = []
        if self.peek() and self.peek()[0] == "RB":
            return values
        values.append(self.parse_value())
        while self.peek() and self.peek()[0] == "COMMA":
            self.pos += 1
            values.append(self.parse_value())
        return values


def parse_dsl(src: str) -> Any:
    """Tokenise + parse a DSL string. Raises ``DSLError`` on failure."""
    if not src or not src.strip():
        raise DSLError("empty rule")
    return _Parser(_tokenise(src)).parse()


# ── Evaluator ────────────────────────────────────────────────────────────


def _field_value(entry: AuditEntry, name: str) -> Any:
    """Resolve a field path on an audit entry. Supports nested
    ``payload.foo.bar`` access."""
    n = name.lower()
    if n == "actor":
        return entry.actor
    if n == "action":
        return entry.action
    if n in ("resource", "target"):
        return entry.target
    if n == "timestamp":
        return entry.timestamp
    if n == "seq":
        return entry.seq
    if n == "severity":
        return _severity_of(entry)
    if n.startswith("payload."):
        cur: Any = entry.payload
        for part in name.split(".")[1:]:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur
    if n == "payload":
        return entry.payload
    return None


def _severity_of(entry: AuditEntry) -> str:
    if isinstance(entry.payload, dict) and "severity" in entry.payload:
        return str(entry.payload["severity"]).lower()
    a = (entry.action or "").lower()
    if any(k in a for k in ("tamper", "denied", "unauthorized", "locked", "rbac_denied")):
        return "critical"
    if any(k in a for k in ("delete", "ban", "kick", "revoke", "purge")):
        return "high"
    if any(k in a for k in ("failed", "error", "rate_limited")):
        return "medium"
    if any(k in a for k in ("login", "logout", "token", "permission")):
        return "low"
    return "info"


def _compare(lhs: Any, op: str, rhs: Any) -> bool:
    # Severity-aware ordering for the magic "severity" field
    if isinstance(lhs, str) and lhs.lower() in SEVERITY_RANK \
       and isinstance(rhs, str) and rhs.lower() in SEVERITY_RANK:
        a = SEVERITY_RANK[lhs.lower()]
        b = SEVERITY_RANK[rhs.lower()]
        if op == "=":  return a == b
        if op == "!=": return a != b
        if op == ">=": return a >= b
        if op == "<=": return a <= b
        if op == ">":  return a > b
        if op == "<":  return a < b

    try:
        if op == "=":  return lhs == rhs
        if op == "!=": return lhs != rhs
        if op == ">=": return lhs >= rhs
        if op == "<=": return lhs <= rhs
        if op == ">":  return lhs > rhs
        if op == "<":  return lhs < rhs
        if op == "IN":     return lhs in (rhs or [])
        if op == "NOT IN": return lhs not in (rhs or [])
    except TypeError:
        return False
    return False


class _WithinState:
    """Sliding-window counter keyed by AST id; used for WITHIN rules.
    Maintains a deque of (timestamp, entry_seq) pairs trimmed on access."""
    def __init__(self) -> None:
        self.windows: dict[int, deque[tuple[float, int]]] = {}

    def hit(self, key: int, ts: float, seq: int, window_secs: int) -> int:
        dq = self.windows.setdefault(key, deque())
        dq.append((ts, seq))
        cutoff = ts - window_secs
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        return len(dq)


_within_state = _WithinState()


def _evaluate(node: Any, entry: AuditEntry) -> bool:
    if isinstance(node, _And):
        return _evaluate(node.left, entry) and _evaluate(node.right, entry)
    if isinstance(node, _Or):
        return _evaluate(node.left, entry) or _evaluate(node.right, entry)
    if isinstance(node, _Not):
        return not _evaluate(node.inner, entry)
    if isinstance(node, _Predicate):
        lhs = _field_value(entry, node.field)
        return _compare(lhs, node.op, node.value)
    if isinstance(node, _Within):
        # First check inner predicate; if it matches, register a window
        # hit and require >= threshold inside the window.
        if not _evaluate(node.inner, entry):
            return False
        count = _within_state.hit(id(node), entry.timestamp,
                                  entry.seq, node.seconds)
        return count >= node.threshold
    raise DSLError(f"unknown AST node {type(node).__name__}")


# ── Engine ───────────────────────────────────────────────────────────────


@dataclass
class CompiledRule:
    id: str
    name: str
    severity: str
    channels: list[str]
    ast: Any
    enabled: bool = True
    hit_count: int = 0
    raw_dsl: str = ""


class AlertRulesEngine:
    """Process-wide cache of compiled rules.

    The cache is loaded lazily from the DB and refreshed via
    ``reload()`` whenever CRUD operations change rules. Rule
    evaluation against an incoming entry is O(n_rules) — acceptable
    for the operator-curated rule sets seen in practice (≤ ~200).
    """

    def __init__(self) -> None:
        self._rules: list[CompiledRule] = []
        self._loaded = False

    def compile(self, raw: str) -> Any:
        return parse_dsl(raw)

    def set_rules(self, rules: Iterable[CompiledRule]) -> None:
        self._rules = [r for r in rules if r.enabled]
        self._loaded = True

    @property
    def loaded(self) -> bool:
        return self._loaded

    def rules(self) -> list[CompiledRule]:
        return list(self._rules)

    def evaluate(self, entry: AuditEntry) -> list[CompiledRule]:
        """Return list of rules whose AST evaluates to True for the entry."""
        matched: list[CompiledRule] = []
        for rule in self._rules:
            try:
                if _evaluate(rule.ast, entry):
                    matched.append(rule)
                    rule.hit_count += 1
            except Exception as exc:
                logger.debug("rule_eval_failed",
                             rule_id=rule.id, error=str(exc))
        return matched

    def dry_run(self, ast: Any, entries: Iterable[AuditEntry]) -> dict[str, Any]:
        """Evaluate ``ast`` against ``entries``, returning a summary."""
        matched: list[dict[str, Any]] = []
        scanned = 0
        for entry in entries:
            scanned += 1
            try:
                if _evaluate(ast, entry):
                    matched.append({
                        "seq": entry.seq,
                        "timestamp": entry.timestamp,
                        "actor": entry.actor,
                        "action": entry.action,
                        "resource": entry.target,
                    })
            except Exception:
                pass
        return {
            "scanned": scanned,
            "matched_count": len(matched),
            "samples": matched[:50],
        }


_engine_singleton: Optional[AlertRulesEngine] = None


def get_engine() -> AlertRulesEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = AlertRulesEngine()
    return _engine_singleton


__all__ = [
    "AlertRulesEngine",
    "CompiledRule",
    "DSLError",
    "parse_dsl",
    "get_engine",
    "SEVERITY_RANK",
]
