"""
app.domains.calls — Voice/video signaling, SFU bridge, recording, moderation.

Existing implementation locations:
    app.api.routes.calls               — /api/calls/* + channel_call_router
    app.api.routes.sfu_events          — internal SFU → Python callback
    app.api.routes.turn                — TURN credential mint
    app.services.call_service          — call lifecycle
    app.services.call_signal_authz     — signal-level authorization
    app.services.call_recording        — recording orchestrator
    app.services.call_state_persistence — durable call state
    app.services.call_participant_batcher — fan-out batcher
    app.services.sfu_launcher          — mediasoup worker spawn
    app.models.active_call / call_log  — ORM models
"""

from __future__ import annotations

from app.domains._safe_import import safe_import, safe_module

_exports: dict = {}


def _add_router(modpath: str, alias: str) -> None:
    got = safe_import(modpath, ["router"])
    if "router" in got:
        _exports[alias] = got["router"]


_add_router("app.api.routes.calls",      "calls_router")
_add_router("app.api.routes.sfu_events", "sfu_events_router")
_add_router("app.api.routes.turn",       "turn_router")
_add_router("app.api.routes.voice_messages", "voice_messages_router")
_add_router("app.api.routes.transcription",  "transcription_router")

# calls module exports channel_call_router too
_exports.update(safe_import(
    "app.api.routes.calls",
    ["channel_call_router"],
))

# Services
_exports.update(safe_import(
    "app.services.call_service",
    [
        "CallService",
        "start_call",
        "join_call",
        "leave_call",
        "end_call",
    ],
))
_exports.update(safe_import(
    "app.services.call_signal_authz",
    ["authorize_signal", "AuthzError"],
))
_exports.update(safe_import(
    "app.services.call_recording",
    ["CallRecorder", "start_recording", "stop_recording"],
))
_exports.update(safe_import(
    "app.services.call_state_persistence",
    ["CallStatePersistence", "snapshot_call", "restore_call"],
))
_exports.update(safe_import(
    "app.services.call_participant_batcher",
    ["ParticipantBatcher"],
))
_exports.update(safe_import(
    "app.services.sfu_launcher",
    ["SFULauncher", "launch_sfu_worker", "stop_sfu_worker"],
))

# Models
_exports.update(safe_import("app.models.active_call", ["ActiveCall"]))
_exports.update(safe_import("app.models.call_log",    ["CallLog"]))
_exports.update(safe_import("app.models.voice_message", ["VoiceMessage"]))

# Socket.IO call handlers
_h = safe_module("app.socket.call_handlers")
if _h is not None:
    _exports["call_handlers"] = _h

globals().update(_exports)
__all__ = sorted(_exports.keys())
