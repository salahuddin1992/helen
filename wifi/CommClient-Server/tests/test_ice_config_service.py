"""
Unit tests for :mod:`app.services.ice_config_service`.

Covers:
  - Default STUN + TURN URI generation from the auto-detected LAN IP
  - Explicit ``STUN_URIS`` / ``TURN_URIS`` override path
  - Optional TLS (turns:) inclusion when ``TURN_ENABLE_TLS`` is set
  - ``ICE_FORCE_RELAY`` reflected as ``ice_transport_policy="relay"``
  - Short-term TURN credentials are populated on the TURN entry
  - ``ICE_ANNOUNCED_IP`` overrides auto-detection
  - Graceful degradation when the TURN service raises
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services import ice_config_service as ics


@pytest.fixture(autouse=True)
def _reset_caches():
    ics._reset_lan_ip_cache()
    get_settings.cache_clear()
    yield
    ics._reset_lan_ip_cache()
    get_settings.cache_clear()


@pytest.fixture
def settings_override(monkeypatch):
    """Convenience: set multiple Settings fields then clear the lru_cache."""

    def _apply(**kwargs):
        for k, v in kwargs.items():
            monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()
        # re-import cached settings reference inside ics
        ics.settings = get_settings()
        return ics.settings

    return _apply


# ── Default behavior ──────────────────────────────────────────────────────────


def test_default_returns_stun_and_turn(settings_override, monkeypatch):
    settings_override(
        ICE_ANNOUNCED_IP="10.0.0.5",
        STUN_URIS="",
        TURN_URIS="",
        TURN_ENABLE_TLS="False",
        ICE_FORCE_RELAY="False",
    )
    cfg = ics.build_ice_config("user-1")
    assert cfg["ice_transport_policy"] == "all"
    assert isinstance(cfg["ice_servers"], list)
    assert len(cfg["ice_servers"]) == 2

    stun = cfg["ice_servers"][0]
    turn = cfg["ice_servers"][1]

    assert stun["urls"] == ["stun:10.0.0.5:3478"]
    # No credentials on STUN entry
    assert "username" not in stun
    assert "credential" not in stun

    assert "turn:10.0.0.5:3478?transport=udp" in turn["urls"]
    assert "turn:10.0.0.5:3478?transport=tcp" in turn["urls"]
    assert turn["username"]
    assert turn["credential"]
    # Short-term credential format: <expiry>:<user>
    assert ":user-1" in turn["username"]


def test_turn_credentials_are_user_scoped(settings_override):
    settings_override(ICE_ANNOUNCED_IP="10.0.0.5")
    a = ics.build_ice_config("alice")
    b = ics.build_ice_config("bob")
    turn_a = a["ice_servers"][1]
    turn_b = b["ice_servers"][1]
    assert turn_a["username"] != turn_b["username"]
    assert turn_a["credential"] != turn_b["credential"]


def test_force_relay_policy(settings_override):
    settings_override(ICE_FORCE_RELAY="True", ICE_ANNOUNCED_IP="10.0.0.5")
    cfg = ics.build_ice_config("user-1")
    assert cfg["ice_transport_policy"] == "relay"


def test_tls_uri_included_when_enabled(settings_override):
    settings_override(
        ICE_ANNOUNCED_IP="10.0.0.5",
        TURN_ENABLE_TLS="True",
        TURN_TLS_PORT="5349",
    )
    cfg = ics.build_ice_config("user-1")
    urls = cfg["ice_servers"][1]["urls"]
    assert any(u.startswith("turns:") for u in urls)
    assert any("5349" in u for u in urls)


# ── Overrides ─────────────────────────────────────────────────────────────────


def test_explicit_stun_uris_override(settings_override):
    settings_override(
        STUN_URIS="stun:stun1.example.com:3478, stun:stun2.example.com:3478",
        ICE_ANNOUNCED_IP="10.0.0.5",
    )
    cfg = ics.build_ice_config("user-1")
    stun = cfg["ice_servers"][0]
    assert stun["urls"] == [
        "stun:stun1.example.com:3478",
        "stun:stun2.example.com:3478",
    ]


def test_explicit_turn_uris_override(settings_override):
    settings_override(
        TURN_URIS="turn:relay.example.com:3478?transport=udp",
        ICE_ANNOUNCED_IP="10.0.0.5",
    )
    cfg = ics.build_ice_config("user-1")
    turn = cfg["ice_servers"][1]
    assert turn["urls"] == ["turn:relay.example.com:3478?transport=udp"]
    # Credentials are still attached
    assert turn["username"] and turn["credential"]


def test_announced_ip_overrides_autodetect(settings_override):
    settings_override(ICE_ANNOUNCED_IP="203.0.113.7")
    assert ics.announced_ip() == "203.0.113.7"


# ── Graceful degradation ──────────────────────────────────────────────────────


def test_turn_failure_degrades_to_stun_only(monkeypatch, settings_override):
    settings_override(ICE_ANNOUNCED_IP="10.0.0.5")

    def boom(*_a, **_kw):
        raise RuntimeError("TURN service down")

    monkeypatch.setattr(ics.turn_service, "generate_credentials", boom)
    cfg = ics.build_ice_config("user-1")
    assert len(cfg["ice_servers"]) == 1
    assert cfg["ice_servers"][0]["urls"][0].startswith("stun:")


def test_custom_ttl_propagates(settings_override):
    settings_override(ICE_ANNOUNCED_IP="10.0.0.5")
    cfg = ics.build_ice_config("user-1", ttl_seconds=120)
    assert cfg["ttl_seconds"] == 120
