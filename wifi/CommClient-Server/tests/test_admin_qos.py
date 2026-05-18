"""
Tests for the Voice/Video QoS Live View admin backend.

Coverage:
  * MOS / R-factor unit tests across edge cases & codec table.
  * Anomaly detector with synthetic stream samples.
  * Auth: 401 for missing token, 403 for non-admin role.
  * /calls/active and /calls/{id} REST.
  * Force-preset override (success + 404 paths).
  * Chaos inject validation.
  * WebSocket connect + first frame (`hello`).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.core.deps import get_db
from app.core.security import create_access_token
from app.db.base import Base
from app.main import create_app
from app.services.qos.admin_overrides import qos_admin_overrides
from app.services.qos.anomaly_detector import qos_anomaly_detector
from app.services.qos.mos_calculator import MOSCalculator
from app.services.qos.stats_collector import StreamSample, qos_stats_collector


# =====================================================================
# Pure unit tests — MOS math
# =====================================================================

class TestMOS:
    def test_zero_impairment_yields_top_mos(self):
        # No loss, no latency, opus → R near 82.2 → MOS in the "excellent" band.
        r = MOSCalculator.compute_r_factor(0.0, 0.0, "opus")
        mos = MOSCalculator.mos_from_r(r)
        assert 4.0 < mos <= 4.5
        assert MOSCalculator.quality_label(mos) in ("excellent", "good")

    def test_high_loss_collapses_mos(self):
        r = MOSCalculator.compute_r_factor(0.0, 20.0, "opus")
        mos = MOSCalculator.mos_from_r(r)
        assert mos < 3.5

    def test_high_latency_kicks_in_past_177ms(self):
        # Step kicks in past 177.3 ms → MOS drops sharply.
        low = MOSCalculator.compute_id(150.0)
        high = MOSCalculator.compute_id(300.0)
        assert high > low + 5  # the second term should add a big chunk

    def test_codec_table_lookup_known_codecs(self):
        for c in ("opus", "pcma", "pcmu", "g722", "speex", "aac"):
            ie, bpl = MOSCalculator.codec_impairment(c)
            assert ie > 0 and bpl > 0

    def test_unknown_codec_falls_back_to_opus(self):
        assert MOSCalculator.codec_impairment("magicodec") == MOSCalculator.codec_impairment("opus")

    def test_codec_table_specific_values(self):
        # Match the spec table verbatim.
        assert MOSCalculator.codec_impairment("opus")  == (11.0, 25.0)
        assert MOSCalculator.codec_impairment("pcma")  == (8.0,  24.0)
        assert MOSCalculator.codec_impairment("pcmu")  == (8.0,  24.0)
        assert MOSCalculator.codec_impairment("g722")  == (13.0, 24.0)
        assert MOSCalculator.codec_impairment("speex") == (15.0, 20.0)
        assert MOSCalculator.codec_impairment("aac")   == (10.0, 22.0)

    def test_mos_clipping(self):
        assert MOSCalculator.mos_from_r(-10) == 1.0
        assert MOSCalculator.mos_from_r(150) == 4.5

    def test_r_factor_clamped_to_0_100(self):
        # Catastrophic conditions → R hits the 0 floor.
        r = MOSCalculator.compute_r_factor(2000.0, 90.0, "opus")
        assert 0.0 <= r <= 100.0

    def test_compute_mos_dataclass_round_trip(self):
        result = MOSCalculator.compute_mos(jitter_ms=20, loss_pct=2, rtt_ms=100, codec="opus")
        d = result.to_dict()
        assert "mos" in d and "r_factor" in d and d["codec"] == "opus"
        assert d["quality_label"] in ("excellent", "good", "fair", "poor", "bad")

    def test_quality_band_thresholds_monotonic(self):
        # Bands must be sorted descending so the first-match lookup works.
        thresholds = [t for t, _ in MOSCalculator.QUALITY_BANDS]
        assert thresholds == sorted(thresholds, reverse=True)


# =====================================================================
# Anomaly detector — synthetic stream injection
# =====================================================================

def _inject_sample(call_id: str, pid: str, *, loss=0.0, jitter=0.0, rtt=0.0,
                   mos=4.0, bitrate=300.0, kind="audio", direction="outbound"):
    """Push a single StreamSample into the in-memory collector buffer."""
    from collections import deque
    from app.services.qos.stats_collector import CallTelemetry

    tele = qos_stats_collector._calls.setdefault(
        call_id, CallTelemetry(call_id=call_id),
    )
    key = (pid, f"{kind}:{direction}")
    buf = tele.streams.setdefault(key, deque(maxlen=300))
    buf.append(StreamSample(
        timestamp=0.0, kind=kind, direction=direction, codec="opus",
        bitrate_kbps=bitrate, packets_sent=1000, packets_lost=int(loss * 10),
        packet_loss_pct=loss, jitter_ms=jitter, rtt_ms=rtt,
        fps=None, resolution=None, frames_dropped=0,
        nack_count=0, pli_count=0, fir_count=0,
        audio_level=None, mos=mos, r_factor=mos * 20, raw={},
    ))


class TestAnomalies:
    def setup_method(self):
        qos_stats_collector.reset()

    def test_sustained_loss(self):
        cid = "callA"
        for _ in range(6):
            _inject_sample(cid, "u1", loss=10.0)
        anns = qos_anomaly_detector.detect(cid)
        codes = {a.code for a in anns}
        assert "loss_sustained" in codes

    def test_jitter_high(self):
        cid = "callB"
        for _ in range(6):
            _inject_sample(cid, "u1", jitter=200.0)
        codes = {a.code for a in qos_anomaly_detector.detect(cid)}
        assert "jitter_high" in codes

    def test_rtt_high(self):
        cid = "callC"
        for _ in range(6):
            _inject_sample(cid, "u1", rtt=800.0)
        codes = {a.code for a in qos_anomaly_detector.detect(cid)}
        assert "rtt_high" in codes

    def test_mos_collapsed(self):
        cid = "callD"
        for _ in range(12):
            _inject_sample(cid, "u1", mos=2.0)
        codes = {a.code for a in qos_anomaly_detector.detect(cid)}
        assert "mos_collapsed" in codes

    def test_bandwidth_collapse(self):
        cid = "callE"
        _inject_sample(cid, "u1", bitrate=2000)
        for _ in range(4):
            _inject_sample(cid, "u1", bitrate=300)
        # Last sample is 15% of peak → triggers bandwidth_collapse.
        codes = {a.code for a in qos_anomaly_detector.detect(cid)}
        assert "bandwidth_collapse" in codes

    def test_no_anomalies_on_healthy_stream(self):
        cid = "callF"
        for _ in range(6):
            _inject_sample(cid, "u1", loss=0.1, jitter=5, rtt=40, mos=4.3)
        anns = qos_anomaly_detector.detect(cid)
        # Only acceptable codes are absent (or empty)
        for a in anns:
            assert a.code not in (
                "loss_sustained", "jitter_high", "rtt_high",
                "mos_collapsed", "mos_low",
            )


# =====================================================================
# HTTP / WebSocket integration tests
# =====================================================================

# A dedicated fixture set — we need a *sync* TestClient for WebSocket support
# (httpx async transport doesn't yet do WS), so we build our own DB-backed app.

@pytest.fixture
def sync_admin_app():
    """Returns (TestClient, admin_token)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_setup())

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    admin_token = create_access_token("admin-uid", role="admin")
    yield TestClient(app), admin_token

    async def _teardown():
        await engine.dispose()
    try:
        asyncio.get_event_loop().run_until_complete(_teardown())
    except Exception:
        pass


class TestAuth:
    def test_active_calls_requires_auth(self, sync_admin_app):
        client, _ = sync_admin_app
        r = client.get("/api/admin/calls/active")
        assert r.status_code in (401, 403)

    def test_active_calls_rejects_non_admin(self, sync_admin_app):
        client, _ = sync_admin_app
        token = create_access_token("u-1", role="user")
        r = client.get(
            "/api/admin/calls/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403


class TestActiveCalls:
    def setup_method(self):
        qos_stats_collector.reset()
        qos_admin_overrides.reset()
        # Wipe call_service singleton state between tests.
        from app.services.call_service import call_service
        call_service._active_calls.clear()                              # noqa: SLF001
        call_service._user_calls.clear()                                # noqa: SLF001

    def test_active_calls_empty_list(self, sync_admin_app):
        client, token = sync_admin_app
        r = client.get(
            "/api/admin/calls/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json() == {"calls": [], "count": 0}

    def test_call_detail_404_when_unknown(self, sync_admin_app):
        client, token = sync_admin_app
        r = client.get(
            "/api/admin/calls/nope/stats",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "call_not_found"


# =====================================================================
# Override + chaos
# =====================================================================

class TestOverrides:
    def setup_method(self):
        qos_stats_collector.reset()
        qos_admin_overrides.reset()
        from app.services.call_service import call_service
        call_service._active_calls.clear()                              # noqa: SLF001
        call_service._user_calls.clear()                                # noqa: SLF001

    def _spawn_call(self, call_id="callX", initiator="u1", peers=("u2",)):
        from app.services.call_service import ActiveCall, call_service
        c = ActiveCall(call_id, initiator, "video", "mesh", channel_id=None)
        c.add_participant(initiator)
        for p in peers:
            c.add_participant(p)
        c.status = "active"
        c.started_at = datetime.now(timezone.utc)
        call_service._active_calls[call_id] = c                         # noqa: SLF001
        call_service._user_calls[initiator] = call_id                   # noqa: SLF001
        for p in peers:
            call_service._user_calls[p] = call_id                       # noqa: SLF001
        return c

    def test_force_preset_404_on_missing_call(self, sync_admin_app):
        client, token = sync_admin_app
        r = client.post(
            "/api/admin/calls/nope/force-preset",
            headers={"Authorization": f"Bearer {token}"},
            json={"participant_id": "u1", "preset": "low"},
        )
        assert r.status_code == 404

    def test_force_preset_invalid_preset(self, sync_admin_app):
        client, token = sync_admin_app
        self._spawn_call()
        r = client.post(
            "/api/admin/calls/callX/force-preset",
            headers={"Authorization": f"Bearer {token}"},
            json={"participant_id": "u1", "preset": "ultra-mega"},
        )
        assert r.status_code == 400

    def test_force_preset_emits_to_socket(self, sync_admin_app, monkeypatch):
        client, token = sync_admin_app
        self._spawn_call()

        emitted = []

        async def fake_emit(event, data, user_id):
            emitted.append((event, data, user_id))

        monkeypatch.setattr("app.socket.server.emit_to_user", fake_emit)

        r = client.post(
            "/api/admin/calls/callX/force-preset",
            headers={"Authorization": f"Bearer {token}"},
            json={"participant_id": "u2", "preset": "low"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "force_preset"
        assert body["payload"]["preset"] == "low"
        assert body["delivered"] is True
        assert emitted and emitted[0][0] == "qos:force_preset"
        assert emitted[0][2] == "u2"

    def test_chaos_inject_requires_one_param(self, sync_admin_app):
        client, token = sync_admin_app
        self._spawn_call()
        r = client.post(
            "/api/admin/calls/callX/chaos",
            headers={"Authorization": f"Bearer {token}"},
            json={"participant_id": "u2"},
        )
        assert r.status_code == 400

    def test_chaos_inject_happy_path(self, sync_admin_app, monkeypatch):
        client, token = sync_admin_app
        self._spawn_call()
        async def fake_emit(event, data, user_id):
            return None
        monkeypatch.setattr("app.socket.server.emit_to_user", fake_emit)

        r = client.post(
            "/api/admin/calls/callX/chaos",
            headers={"Authorization": f"Bearer {token}"},
            json={"participant_id": "u2", "loss_pct": 5.0, "latency_ms": 200},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["payload"]["loss_pct"] == 5.0
        assert body["payload"]["latency_ms"] == 200


# =====================================================================
# WebSocket
# =====================================================================

class TestWebSocket:
    def setup_method(self):
        qos_stats_collector.reset()
        from app.services.call_service import call_service
        call_service._active_calls.clear()                              # noqa: SLF001

    def test_ws_rejects_missing_token(self, sync_admin_app):
        client, _ = sync_admin_app
        try:
            with client.websocket_connect("/api/admin/ws/qos"):
                pass
            pytest.fail("expected websocket close")
        except Exception:
            pass

    def test_ws_accepts_admin_and_sends_hello(self, sync_admin_app):
        client, token = sync_admin_app
        with client.websocket_connect(f"/api/admin/ws/qos?token={token}") as ws:
            first = ws.receive_json()
            assert first["type"] == "hello"
            assert first["user_id"] == "admin-uid"
