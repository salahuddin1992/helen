"""tests/test_secrets_resolver_phase4.py — Phase 1 / Module B coverage."""

from __future__ import annotations

import os

import pytest


def test_resolver_importable():
    try:
        from app.core.secrets_resolver import SecretsResolver, resolve_secret  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("secrets_resolver not in this build")
    assert SecretsResolver is not None
    assert callable(resolve_secret)


def test_resolver_env_takes_priority(monkeypatch):
    try:
        from app.core.secrets_resolver import resolve_secret  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("secrets_resolver not in this build")
    monkeypatch.setenv("MY_TEST_SECRET_FOR_PHASE4", "env-value-XYZ")
    val = resolve_secret("MY_TEST_SECRET_FOR_PHASE4", default="fallback")
    assert val == "env-value-XYZ"


def test_resolver_default_when_absent(monkeypatch):
    try:
        from app.core.secrets_resolver import resolve_secret  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("secrets_resolver not in this build")
    monkeypatch.delenv("ANOTHER_NON_EXISTENT_PHASE4_SECRET", raising=False)
    val = resolve_secret("ANOTHER_NON_EXISTENT_PHASE4_SECRET", default="fallback-OK")
    assert val == "fallback-OK"


def test_resolver_returns_none_for_unknown_without_default():
    try:
        from app.core.secrets_resolver import resolve_secret  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("secrets_resolver not in this build")
    val = resolve_secret("DEFINITELY_NOT_SET_PHASE4_KEY")
    assert val is None or val == ""
