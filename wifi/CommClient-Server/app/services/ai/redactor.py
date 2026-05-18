"""
PII redactor — strips sensitive identifiers from any prompt before it
leaves the server to an external LLM. Returns a redaction map so the
caller can un-redact the response on the way back.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"\+?\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{2,4}[\s\-.]?\d{2,4}[\s\-.]?\d{0,4}"
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# URL with embedded credentials, e.g. https://user:pass@example.com/foo
_AUTH_URL_RE = re.compile(r"\bhttps?://[^\s/:@]+:[^\s/:@]+@[^\s]+")
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class RedactionReport:
    text: str
    map: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def restore(self, response: str) -> str:
        """Reverse the redactions inside the LLM response. Only restores
        tokens this report actually produced — order matters: emails first
        (more specific), phones last."""
        out = response
        for token, original in self.map.items():
            out = out.replace(token, original)
        return out


@dataclass
class RedactorPolicy:
    strict: bool = False
    strict_max_total: int = 25
    allow_kinds: set[str] = field(default_factory=lambda: {
        "email", "phone", "cc", "ip", "ssn", "auth_url",
    })


class _Counter:
    def __init__(self) -> None:
        self._n: dict[str, int] = {}

    def next(self, kind: str) -> str:
        self._n[kind] = self._n.get(kind, 0) + 1
        return f"<{kind.upper()}_{self._n[kind]}>"

    def total(self) -> int:
        return sum(self._n.values())


def redact(text: str, policy: RedactorPolicy | None = None) -> RedactionReport:
    """Replace PII matches in ``text`` with stable placeholders."""
    policy = policy or RedactorPolicy()
    c = _Counter()
    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}

    def _sub(rx: re.Pattern[str], kind: str,
             extra_check=None) -> None:
        if kind not in policy.allow_kinds:
            return
        nonlocal out
        def _repl(m: re.Match[str]) -> str:
            orig = m.group(0)
            if extra_check is not None and not extra_check(orig):
                return orig
            tok = c.next(kind)
            mapping[tok] = orig
            counts[kind] = counts.get(kind, 0) + 1
            return tok
        out = rx.sub(_repl, out)

    out = text
    _sub(_AUTH_URL_RE, "auth_url")
    _sub(_EMAIL_RE, "email")
    _sub(_SSN_RE, "ssn")
    _sub(_CC_RE, "cc", extra_check=_luhn_ok)
    _sub(_PHONE_RE, "phone")
    _sub(_IPV4_RE, "ip")

    if policy.strict and c.total() > policy.strict_max_total:
        raise PIILeakBlocked(
            f"too many PII patterns ({c.total()}) — refusing to send"
        )

    return RedactionReport(text=out, map=mapping, counts=counts)


def redact_batch(texts: Iterable[str], policy: RedactorPolicy | None = None) -> list[RedactionReport]:
    return [redact(t, policy) for t in texts]


class PIILeakBlocked(RuntimeError):
    pass
