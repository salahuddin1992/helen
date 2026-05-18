"""Unit tests for Group 3 operability modules:
   * stun_responder
   * federation_autodiscovery
   * backup_verifier
   * federation_shaper

Each module is independent and gated by its own env var, so the tests
exercise them in isolation."""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import struct
import time
from pathlib import Path

import pytest


# ── stun_responder ───────────────────────────────────────────────


def test_build_binding_response_round_trip():
    """Build a binding request, run it through build_binding_response,
    parse the reply and confirm the XOR-MAPPED-ADDRESS matches."""
    from app.services.stun_responder import build_binding_response
    from app.services.turn_health import _parse_attrs, _decode_xor_address

    txid = b"\x11" * 12
    magic = 0x2112A442
    req = (struct.pack("!HH", 0x0001, 0)
           + struct.pack("!I", magic) + txid)
    reply = build_binding_response(req, ("10.0.0.42", 33445))

    method, length = struct.unpack("!HH", reply[:4])
    assert method == 0x0101  # binding response
    attrs = _parse_attrs(reply[20:20 + length])
    xa = attrs.get(0x0020)
    assert xa is not None
    decoded = _decode_xor_address(xa, txid)
    assert decoded == ("10.0.0.42", 33445)


def test_build_binding_response_rejects_short_input():
    from app.services.stun_responder import (
        build_binding_response, build_binding_error,
    )
    assert build_binding_response(b"\x00" * 4, ("1.1.1.1", 1)) == b""
    assert build_binding_error(b"\x00" * 4) == b""


@pytest.mark.asyncio
async def test_stun_responder_end_to_end():
    """Spin up the full responder on 127.0.0.1, send a real binding
    request from a UDP socket, and verify we get a valid response."""
    from app.services.stun_responder import (
        configure_stun_responder, shutdown_stun_responder,
    )
    from app.services.turn_health import (
        _parse_attrs, _decode_xor_address,
    )

    srv = configure_stun_responder("127.0.0.1", 0)
    await srv.start()
    try:
        # Pull the actual bound port out of the underlying socket.
        sock = srv._transport.get_extra_info("socket")  # type: ignore[union-attr]
        port = sock.getsockname()[1]

        # Send a binding request from a fresh UDP socket. Use async
        # socket I/O — a blocking recvfrom would starve the event
        # loop and prevent the responder's datagram_received from
        # firing.
        loop = asyncio.get_running_loop()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.setblocking(False)
        try:
            txid = b"\x22" * 12
            magic = 0x2112A442
            req = (struct.pack("!HH", 0x0001, 0)
                   + struct.pack("!I", magic) + txid)
            await loop.sock_sendto(client, req, ("127.0.0.1", port))
            data, _ = await asyncio.wait_for(
                loop.sock_recvfrom(client, 2048), timeout=2.0,
            )
        finally:
            client.close()

        method, length = struct.unpack("!HH", data[:4])
        assert method == 0x0101
        attrs = _parse_attrs(data[20:20 + length])
        ip, _ = _decode_xor_address(attrs[0x0020], txid)  # type: ignore[arg-type]
        assert ip == "127.0.0.1"
        assert srv.stats.requests_total >= 1
        assert srv.stats.responses_total >= 1
    finally:
        await shutdown_stun_responder()


def test_stun_responder_configure_from_env_off_by_default(monkeypatch):
    from app.services.stun_responder import configure_from_env
    monkeypatch.delenv("HELEN_STUN_LISTEN", raising=False)
    assert configure_from_env() is None


# ── federation_autodiscovery ─────────────────────────────────────


def test_fingerprint_secret_is_deterministic_and_short():
    from app.services.federation_autodiscovery import fingerprint_secret
    fp1 = fingerprint_secret("aaa-bbb-ccc")
    fp2 = fingerprint_secret("aaa-bbb-ccc")
    fp3 = fingerprint_secret("different")
    assert fp1 == fp2
    assert fp1 != fp3
    assert len(fp1) == 16
    assert fingerprint_secret("") == ""


def test_candidate_ledger_upsert_and_drain():
    from app.services.federation_autodiscovery import (
        FederationCandidate, _CandidateLedger,
    )
    led = _CandidateLedger()
    c1 = FederationCandidate("srv-1", "10.0.0.1", 3000)
    c2 = FederationCandidate("srv-2", "10.0.0.2", 3000)
    assert led.upsert(c1) is True   # new
    assert led.upsert(c1) is False  # duplicate
    led.upsert(c2)
    assert len(led.all()) == 2
    drained = led.drain()
    assert {c.server_id for c in drained} == {"srv-1", "srv-2"}
    assert led.all() == []


def test_candidate_ledger_evict_stale():
    from app.services.federation_autodiscovery import (
        FederationCandidate, _CandidateLedger,
    )
    led = _CandidateLedger()
    fresh = FederationCandidate("fresh", "10.0.0.1", 3000)
    stale = FederationCandidate("stale", "10.0.0.2", 3000)
    stale.last_seen_at = time.time() - 9999
    led.upsert(fresh)
    led.upsert(stale)
    evicted = led.evict_older_than(60.0)
    assert evicted == 1
    remaining = {c.server_id for c in led.all()}
    assert remaining == {"fresh"}


def test_federation_autodiscover_off_by_default(monkeypatch):
    from app.services.federation_autodiscovery import configure_from_env
    monkeypatch.delenv("HELEN_FEDERATION_AUTODISCOVER", raising=False)
    started = configure_from_env(my_server_id="x", federation_secret="y")
    assert started is False


# ── backup_verifier ──────────────────────────────────────────────


def _make_valid_sqlite(path: Path,
                        tables: list[str] = ("users", "messages")) -> None:
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        for t in tables:
            cur.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, v TEXT)")
            cur.executemany(
                f"INSERT INTO {t} (v) VALUES (?)",
                [("a",), ("b",), ("c",)],
            )
        conn.commit()
    finally:
        conn.close()


def test_verify_backup_file_reports_ok(tmp_path: Path):
    from app.services.backup_verifier import verify_backup_file
    db = tmp_path / "commclient_backup_20260506_010203.db"
    _make_valid_sqlite(db)
    r = verify_backup_file(db, required_tables=("users", "messages"))
    assert r.overall_ok is True
    assert r.integrity_ok is True
    assert r.error is None
    assert {c.table for c in r.table_checks} == {"users", "messages"}
    for c in r.table_checks:
        assert c.ok is True
        assert c.row_count == 3


def test_verify_backup_file_flags_missing_table(tmp_path: Path):
    from app.services.backup_verifier import verify_backup_file
    db = tmp_path / "commclient_backup_20260506_010203.db"
    _make_valid_sqlite(db, tables=["users"])  # no "messages" table
    r = verify_backup_file(db, required_tables=("users", "messages"))
    assert r.integrity_ok is True
    assert r.overall_ok is False
    bad = [c for c in r.table_checks if not c.ok]
    assert len(bad) == 1
    assert bad[0].table == "messages"


def test_verify_backup_file_handles_missing_file(tmp_path: Path):
    from app.services.backup_verifier import verify_backup_file
    r = verify_backup_file(tmp_path / "nope.db")
    assert r.overall_ok is False
    assert r.error and "not found" in r.error


def test_find_latest_backup_picks_newest_name(tmp_path: Path):
    from app.services.backup_verifier import find_latest_backup
    for ts in ("20260101_010101", "20260301_010101", "20260506_010101"):
        (tmp_path / f"commclient_backup_{ts}.db").write_bytes(b"x")
    latest = find_latest_backup(tmp_path)
    assert latest is not None
    assert latest.name == "commclient_backup_20260506_010101.db"


def test_find_latest_backup_returns_none_for_empty_dir(tmp_path: Path):
    from app.services.backup_verifier import find_latest_backup
    assert find_latest_backup(tmp_path) is None
    assert find_latest_backup(tmp_path / "missing") is None


@pytest.mark.asyncio
async def test_backup_verifier_run_once_populates_history(tmp_path: Path):
    from app.services.backup_verifier import (
        configure_backup_verifier, shutdown_backup_verifier,
    )
    db = tmp_path / "commclient_backup_20260506_010203.db"
    _make_valid_sqlite(db)
    v = configure_backup_verifier(
        tmp_path, interval_s=999999, required_tables=("users", "messages"),
    )
    try:
        r = await v.run_once()
        assert r.overall_ok is True
        snap = v.status()
        assert snap["history_size"] == 1
        assert snap["last"]["overall_ok"] is True
    finally:
        await shutdown_backup_verifier()


# ── federation_shaper ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shaper_no_op_when_disabled():
    from app.services.federation_shaper import FederationShaper
    s = FederationShaper(0.0)  # disabled
    waited = await s.acquire("peer-1", 1024 * 1024)
    assert waited == 0.0


@pytest.mark.asyncio
async def test_shaper_immediate_when_bucket_full():
    from app.services.federation_shaper import FederationShaper
    s = FederationShaper(1024.0, burst_bytes=8192.0)  # 1 KiB/s, 8 KiB burst
    # First acquire fits in capacity → no wait.
    waited = await s.acquire("peer-1", 4096)
    assert waited == 0.0


@pytest.mark.asyncio
async def test_shaper_blocks_until_refill():
    """Drain the bucket, then a second acquire must wait for refill."""
    from app.services.federation_shaper import FederationShaper
    s = FederationShaper(2048.0, burst_bytes=2048.0)  # 2 KiB/s, 2 KiB burst
    await s.acquire("peer-A", 2048)               # drains to zero
    t0 = time.perf_counter()
    waited = await s.acquire("peer-A", 1024)      # needs 0.5s of refill
    elapsed = time.perf_counter() - t0
    assert waited > 0.3
    assert elapsed > 0.3
    stats = s.stats_for("peer-A")
    assert stats and stats.bytes_sent == 2048 + 1024
    assert stats.wait_count >= 1


@pytest.mark.asyncio
async def test_shaper_overload_when_wait_exceeds_cap():
    """Massive request against a slow bucket with a tight max_wait."""
    from app.services.federation_shaper import (
        FederationShaper, ShaperOverloaded,
    )
    s = FederationShaper(1024.0, burst_bytes=1024.0, max_wait_s=0.05)
    await s.acquire("peer-X", 1024)               # drain
    with pytest.raises(ShaperOverloaded):
        await s.acquire("peer-X", 1024)           # would need ~1s, cap 0.05s
    stats = s.stats_for("peer-X")
    assert stats and stats.bytes_throttled >= 1024


@pytest.mark.asyncio
async def test_shaper_per_peer_isolation():
    """Draining peer-A's bucket must not throttle peer-B."""
    from app.services.federation_shaper import FederationShaper
    s = FederationShaper(2048.0, burst_bytes=2048.0)
    await s.acquire("peer-A", 2048)               # drain A
    t0 = time.perf_counter()
    waited = await s.acquire("peer-B", 2048)      # full B → no wait
    assert waited == 0.0
    assert (time.perf_counter() - t0) < 0.05


@pytest.mark.asyncio
async def test_shaper_module_acquire_no_op_without_singleton():
    """Use the conftest's session-scoped event loop instead of
    asyncio.run() — calling asyncio.run() in-test closes that loop
    and breaks every async test that runs after this file."""
    from app.services.federation_shaper import (
        acquire, shutdown_federation_shaper,
    )
    shutdown_federation_shaper()
    # Without a configured shaper, the convenience wrapper is a no-op.
    waited = await acquire("anything", 9999)
    assert waited == 0.0
