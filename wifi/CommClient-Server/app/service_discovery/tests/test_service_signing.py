"""Tests for HMAC signing + tamper detection."""

from __future__ import annotations

import time

import pytest

from app.service_discovery.discovery_exceptions import SignatureError
from app.service_discovery.service_record import ServiceRecord, ServiceType
from app.service_discovery.service_signing import (
    fingerprint, require_valid, sign_record, status, verify_record,
)


def _record(host="1.1.1.1") -> ServiceRecord:
    return ServiceRecord(
        service_id="r1",
        service_type=ServiceType.RELAY,
        server_id="peer-A",
        host=host, port=3000,
        cluster_id="default",
    )


def test_sign_then_verify_succeeds():
    r = _record()
    sign_record(r)
    ok, reason = verify_record(r)
    assert ok
    assert reason == "ok"


def test_unsigned_record_rejected():
    r = _record()
    ok, reason = verify_record(r)
    assert not ok
    assert "signature" in reason.lower()


def test_tampered_field_invalidates_signature():
    r = _record()
    sign_record(r)
    r.host = "9.9.9.9"  # tamper after signing
    ok, reason = verify_record(r)
    assert not ok
    assert reason == "signature_mismatch"


def test_stale_signed_at_rejected():
    r = _record()
    sign_record(r)
    r.signed_at = time.time() - 600  # 10 min old
    ok, reason = verify_record(r)
    assert not ok
    assert "stale" in reason.lower()


def test_require_valid_raises_signature_error_on_bad():
    r = _record()
    sign_record(r)
    r.signature = "deadbeef" * 8
    with pytest.raises(SignatureError):
        require_valid(r)


def test_require_valid_passes_for_clean_signature():
    r = _record()
    sign_record(r)
    require_valid(r)  # no exception


def test_fingerprint_is_stable_for_same_secret():
    secret = b"some-secret"
    a = fingerprint(secret)
    b = fingerprint(secret)
    assert a == b
    assert len(a) == 16


def test_fingerprint_changes_with_secret():
    a = fingerprint(b"secret-1")
    b = fingerprint(b"secret-2")
    assert a != b


def test_different_cluster_ids_produce_different_signatures():
    r1 = _record()
    r1.cluster_id = "cluster-a"
    sign_record(r1)
    r2 = _record()
    r2.cluster_id = "cluster-b"
    sign_record(r2)
    # Same canonical fields except cluster — should still produce
    # different signatures because the secret differs.
    assert r1.signature != r2.signature


def test_status_returns_expected_keys():
    s = status()
    assert {"cluster_id", "fingerprint", "replay_window_sec"}.issubset(s.keys())
