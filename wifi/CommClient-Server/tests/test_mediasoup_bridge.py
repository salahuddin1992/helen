"""
Unit tests for MediasoupBridge — verifies that every method posts the right
payload at the right path. We stub ``_http`` to a recording fake so the
worker does not need to be running.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.services.topology_manager import MediasoupBridge


@dataclass
class _FakeResponse:
    status_code: int = 200
    _json: dict = field(default_factory=lambda: {"ok": True})

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


@dataclass
class _FakeHTTP:
    calls: list = field(default_factory=list)
    response_override: dict | None = None

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return _FakeResponse(_json=self.response_override or {"ok": True})

    async def delete(self, url):
        self.calls.append(("DELETE", url, None))
        return _FakeResponse(_json=self.response_override or {"ok": True})

    async def get(self, url):
        self.calls.append(("GET", url, None))
        return _FakeResponse(_json=self.response_override or {"recordings": []})


@pytest.fixture
def bridge_and_fake():
    b = MediasoupBridge("http://127.0.0.1:4443", token="test-token")
    fake = _FakeHTTP()

    async def _http():
        return fake

    b._http = _http  # type: ignore[assignment]
    return b, fake


# ── Bandwidth / simulcast ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_preferred_layers_spatial_only(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.set_preferred_layers("c1", "cons1", spatial_layer=0)
    assert fake.calls == [
        ("POST", "/routers/c1/consumers/cons1/preferred-layers", {"spatial_layer": 0})
    ]


@pytest.mark.asyncio
async def test_set_preferred_layers_spatial_and_temporal(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.set_preferred_layers("c1", "cons1", spatial_layer=2, temporal_layer=1)
    assert fake.calls[0][2] == {"spatial_layer": 2, "temporal_layer": 1}


@pytest.mark.asyncio
async def test_set_max_incoming_bitrate(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.set_max_incoming_bitrate("c1", "t1", 2_500_000)
    assert fake.calls == [
        (
            "POST",
            "/routers/c1/transports/t1/max-incoming-bitrate",
            {"bitrate": 2_500_000},
        )
    ]


@pytest.mark.asyncio
async def test_set_max_outgoing_bitrate(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.set_max_outgoing_bitrate("c1", "t1", 1_200_000)
    assert fake.calls == [
        (
            "POST",
            "/routers/c1/transports/t1/max-outgoing-bitrate",
            {"bitrate": 1_200_000},
        )
    ]


@pytest.mark.asyncio
async def test_set_consumer_priority(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.set_consumer_priority("c1", "cons1", 64)
    assert fake.calls == [
        ("POST", "/routers/c1/consumers/cons1/priority", {"priority": 64})
    ]


# ── Producer pause / resume ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_producer(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.pause_producer("c1", "prod1")
    assert fake.calls == [("POST", "/routers/c1/producers/prod1/pause", None)]


@pytest.mark.asyncio
async def test_resume_producer(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.resume_producer("c1", "prod1")
    assert fake.calls == [("POST", "/routers/c1/producers/prod1/resume", None)]


# ── Audio-level observer ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_audio_observer(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.ensure_audio_observer("c1")
    assert fake.calls == [("POST", "/routers/c1/audio-observer/ensure", None)]


@pytest.mark.asyncio
async def test_audio_observer_add(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.audio_observer_add("c1", "prod-audio")
    assert fake.calls == [
        ("POST", "/routers/c1/audio-observer/add", {"producer_id": "prod-audio"})
    ]


@pytest.mark.asyncio
async def test_audio_observer_remove_is_best_effort(bridge_and_fake):
    """Removal must never raise — it's a cleanup path."""
    b, fake = bridge_and_fake
    # Even with a 500 response we should not raise.
    fake.response_override = None

    async def bad_post(url, json=None):
        raise RuntimeError("connection refused")

    fake.post = bad_post  # type: ignore[assignment]
    # Should not raise
    await b.audio_observer_remove("c1", "prod-audio")


# ── Recording ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_recording_audio_only(bridge_and_fake):
    b, fake = bridge_and_fake
    fake.response_override = {
        "ok": True,
        "recording_id": "rec_123",
        "output_path": "/tmp/call__rec_123.webm",
    }
    result = await b.start_recording(
        "c1", audio_producer_id="p-aud", video_producer_id=None,
    )
    assert result["recording_id"] == "rec_123"
    assert fake.calls[0] == (
        "POST",
        "/routers/c1/recording",
        {"audio_producer_id": "p-aud", "video_producer_id": None, "recording_id": None},
    )


@pytest.mark.asyncio
async def test_start_recording_audio_and_video(bridge_and_fake):
    b, fake = bridge_and_fake
    fake.response_override = {"ok": True, "recording_id": "rec_x", "output_path": "/tmp/x"}
    await b.start_recording(
        "c1", audio_producer_id="pa", video_producer_id="pv", recording_id="rec_x",
    )
    assert fake.calls[0][2] == {
        "audio_producer_id": "pa",
        "video_producer_id": "pv",
        "recording_id": "rec_x",
    }


@pytest.mark.asyncio
async def test_stop_recording_uses_delete(bridge_and_fake):
    b, fake = bridge_and_fake
    await b.stop_recording("c1", "rec_x")
    assert fake.calls == [("DELETE", "/routers/c1/recording/rec_x", None)]


@pytest.mark.asyncio
async def test_list_recordings(bridge_and_fake):
    b, fake = bridge_and_fake
    fake.response_override = {"recordings": []}
    out = await b.list_recordings("c1")
    assert out == {"recordings": []}
    assert fake.calls == [("GET", "/routers/c1/recordings", None)]
