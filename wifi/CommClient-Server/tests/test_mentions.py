"""
Unit tests for :mod:`app.services.message_service` mention parsing.

Focuses on the pure string → username[] extractor — the dispatch path is
covered by integration tests that exercise the DB.
"""

from __future__ import annotations

import pytest

from app.services.message_service import MessageService


# ── Extraction ───────────────────────────────────────────────────────────────


def test_extract_single_mention():
    assert MessageService.extract_mentions("Hi @alice") == ["alice"]


def test_extract_case_insensitive():
    assert MessageService.extract_mentions("Hi @Alice @BOB") == ["alice", "bob"]


def test_dedupe_preserves_order():
    assert MessageService.extract_mentions(
        "@alice @bob @alice and again @bob"
    ) == ["alice", "bob"]


def test_at_the_start_of_string():
    assert MessageService.extract_mentions("@alice first, then @bob") == [
        "alice", "bob",
    ]


def test_email_like_is_not_mention():
    """@foo inside an email address must not be extracted."""
    assert MessageService.extract_mentions(
        "ping me at bob@example.com tomorrow"
    ) == []


def test_short_mention_rejected():
    """Usernames shorter than 2 chars are rejected."""
    assert MessageService.extract_mentions("hi @a") == []


def test_underscore_and_digits_allowed():
    # Dash terminates a username: "@user-02" → "user" (not "user-02")
    assert MessageService.extract_mentions("@user_01 @user-02") == ["user_01", "user"]


def test_long_mention_capped():
    """Greedy match capped at 64 chars — longer input truncates to 64."""
    long_name = "a" * 80
    extracted = MessageService.extract_mentions(f"hey @{long_name}")
    assert extracted == ["a" * 64]


def test_everyone_channel_tokens():
    assert MessageService.extract_mentions("@everyone please review") == ["everyone"]
    assert MessageService.extract_mentions("@channel heads up") == ["channel"]
    assert MessageService.extract_mentions("@here quick Q") == ["here"]


def test_empty_and_none():
    assert MessageService.extract_mentions("") == []
    assert MessageService.extract_mentions(None) == []  # type: ignore[arg-type]


def test_punctuation_after_mention():
    assert MessageService.extract_mentions("thanks @alice, please check.") == [
        "alice"
    ]


def test_two_adjacent_mentions():
    assert MessageService.extract_mentions("@alice@bob") == ["alice"]
    # Second @ has a word character immediately before it, so it's skipped
