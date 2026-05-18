"""
Cross-platform text & mention sanitisation utilities.

Each platform has different markdown flavours and mention syntaxes, so we
normalise to a Helen-internal canonical form and convert on the way out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


# ── Canonicalisation ────────────────────────────────────────

_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_TRIPLE_NL_RE = re.compile(r"\n{3,}")


def canonical(text: str) -> str:
    """Trim, collapse runs of spaces / blank-lines, drop control chars."""
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ord(ch) < 0x20 and ch not in ("\n", "\t"):
            continue
        out.append(ch)
    cleaned = "".join(out)
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    cleaned = _TRIPLE_NL_RE.sub("\n\n", cleaned)
    return cleaned.strip()


# ── Discord ─────────────────────────────────────────────────

_DISCORD_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_DISCORD_CHAN_MENTION_RE = re.compile(r"<#(\d+)>")
_DISCORD_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_DISCORD_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")


def discord_to_helen(text: str, mention_map: dict[str, str] | None = None) -> str:
    """Strip Discord-specific markup and replace mentions with @username."""
    mention_map = mention_map or {}

    def _user(m: re.Match[str]) -> str:
        return "@" + mention_map.get(m.group(1), "user")

    def _emoji(m: re.Match[str]) -> str:
        return f":{m.group(1)}:"

    out = _DISCORD_USER_MENTION_RE.sub(_user, text)
    out = _DISCORD_CHAN_MENTION_RE.sub("#channel", out)
    out = _DISCORD_ROLE_MENTION_RE.sub("@role", out)
    out = _DISCORD_EMOJI_RE.sub(_emoji, out)
    return canonical(out)


def helen_to_discord(text: str, prefix: str = "") -> str:
    """Discord accepts markdown 1:1 — only prefix the sender."""
    body = canonical(text)
    return (prefix + " " + body).strip() if prefix else body


# ── Telegram ────────────────────────────────────────────────

_TG_MD_ESCAPE = "_*[]()~`>#+-=|{}.!"


def helen_to_telegram(text: str, prefix: str = "") -> str:
    body = canonical(text)
    full = f"{prefix} {body}".strip() if prefix else body
    # Telegram MarkdownV2 — escape reserved chars.
    return "".join(("\\" + ch) if ch in _TG_MD_ESCAPE else ch for ch in full)


def telegram_to_helen(text: str) -> str:
    return canonical(text)


# ── Slack ───────────────────────────────────────────────────

_SLACK_USER_RE = re.compile(r"<@([A-Z0-9]+)(\|[^>]+)?>")
_SLACK_CHAN_RE = re.compile(r"<#([A-Z0-9]+)(\|[^>]+)?>")
_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)(\|([^>]+))?>")


def slack_to_helen(text: str, user_map: dict[str, str] | None = None) -> str:
    user_map = user_map or {}

    def _user(m: re.Match[str]) -> str:
        uid = m.group(1)
        return "@" + user_map.get(uid, uid)

    def _link(m: re.Match[str]) -> str:
        return m.group(3) or m.group(1)

    out = _SLACK_USER_RE.sub(_user, text)
    out = _SLACK_CHAN_RE.sub("#channel", out)
    out = _SLACK_LINK_RE.sub(_link, out)
    return canonical(out)


def helen_to_slack(text: str, prefix: str = "") -> dict[str, object]:
    """Return Slack Block Kit payload with username prefix."""
    body = canonical(text)
    return {
        "text": (f"{prefix} {body}".strip() if prefix else body),
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f"*{prefix}* {body}".strip() if prefix else body),
                },
            }
        ],
    }


# ── Per-bridge facade ───────────────────────────────────────

@dataclass
class FormatAdapter:
    kind: str

    def helen_to_remote(self, text: str, prefix: str = "") -> str | dict:
        if self.kind == "discord":
            return helen_to_discord(text, prefix)
        if self.kind == "telegram":
            return helen_to_telegram(text, prefix)
        if self.kind == "slack":
            return helen_to_slack(text, prefix)
        return canonical(text)

    def remote_to_helen(self, text: str, **kw) -> str:
        if self.kind == "discord":
            return discord_to_helen(text, kw.get("mention_map"))
        if self.kind == "telegram":
            return telegram_to_helen(text)
        if self.kind == "slack":
            return slack_to_helen(text, kw.get("user_map"))
        return canonical(text)


def looks_like_loop(text: str, recent: Iterable[str], window: int = 6) -> bool:
    """Cheap echo-loop detector — exact match in last ``window`` outgoings."""
    t = canonical(text)
    return any(canonical(r) == t for r in list(recent)[-window:])
