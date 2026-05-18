"""
Tests for /api/transcripts/* — REST surface for whisper.cpp transcription.

We don't actually invoke whisper-cli (binary may not be installed);
we test routing + auth + storage + the health endpoint.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_endpoint_reports_availability(client, auth_headers):
    r = await client.get("/api/transcripts/health", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "available" in body
    assert "whisper_bin" in body


@pytest.mark.asyncio
async def test_invalid_source_kind_400(client, auth_headers):
    r = await client.post(
        "/api/transcripts/bogus_kind/some_id",
        json={"audio_path": "/tmp/x.wav"},
        headers=auth_headers,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_audio_path_must_exist(client, auth_headers):
    r = await client.post(
        "/api/transcripts/voice_message/abc",
        json={"audio_path": "/tmp/nonexistent-audio-file-xyz.wav"},
        headers=auth_headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_404_when_no_transcript(client, auth_headers):
    r = await client.get(
        "/api/transcripts/voice_message/never-transcribed",
        headers=auth_headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_blocked(client):
    r = await client.get("/api/transcripts/health")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_storage_round_trip(tmp_path):
    """Verify the SQLite store layer directly without spawning whisper.
    Uses pytest's tmp_path so cleanup happens at session end (avoids
    Windows file-lock cleanup races on the inner sqlite3 file)."""
    from app.services.transcription import (
        Transcript, TranscriptSegment, TranscriptStore,
    )
    db_path = tmp_path / "transcripts.db"
    store = TranscriptStore(str(db_path))
    t = Transcript(
        source_id="msg1",
        source_kind="voice_message",
        language="en",
        full_text="hello world",
        segments=[
            TranscriptSegment(start_sec=0.0, end_sec=1.0, text="hello"),
            TranscriptSegment(start_sec=1.0, end_sec=2.0, text="world"),
        ],
        duration_sec=2.0,
    )
    store.save(t)
    retrieved = store.get("voice_message", "msg1")
    assert retrieved is not None
    assert retrieved.full_text == "hello world"
    assert len(retrieved.segments) == 2
    assert retrieved.segments[0].text == "hello"
