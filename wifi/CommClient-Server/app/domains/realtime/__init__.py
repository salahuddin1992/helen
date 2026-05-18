"""
app.domains.realtime — Socket.IO, p2p signaling, overlay transports, SFU events.

Existing implementation locations:
    app.socket.server                — Socket.IO ASGI server instance
    app.socket.*                     — per-feature handler modules
    app.socket.rate_limiter          — per-namespace rate limiting
    app.socket.lan_origin_patch      — LAN-CORS hook
    app.transports.signal            — multi-transport signal bus
    app.transports.bridge            — transport bridge
"""

from __future__ import annotations

from app.domains._safe_import import safe_import, safe_module

_exports: dict = {}

# Socket.IO server instance
_exports.update(safe_import(
    "app.socket.server",
    ["sio", "socket_app", "register_handlers"],
))

# Per-feature handler modules (re-export entire module as attribute)
_HANDLER_MODULES = [
    "auth_handlers",
    "chat_handlers",
    "call_handlers",
    "presence_handlers",
    "voice_handlers",
    "e2ee_handlers",
    "file_drop_handlers",
    "group_file_handlers",
    "notification_handlers",
    "pair_handlers",
    "screen_handlers",
    "sync_handlers",
    "topology_handlers",
    "transport_handlers",
    "whiteboard_handlers",
    "server_fabric_handlers",
    "channel_room",
    "rate_limiter",
    "lan_origin_patch",
]
for _name in _HANDLER_MODULES:
    _mod = safe_module(f"app.socket.{_name}")
    if _mod is not None:
        _exports[_name] = _mod

# Transports
_exports.update(safe_import(
    "app.transports.signal",
    ["SignalBus", "publish_signal", "subscribe_signal"],
))
_exports.update(safe_import(
    "app.transports.bridge",
    ["TransportBridge"],
))
_exports.update(safe_import(
    "app.transports.registry",
    ["TransportRegistry", "get_transport_registry"],
))
_exports.update(safe_import(
    "app.transports.detector",
    ["TransportDetector"],
))

globals().update(_exports)
__all__ = sorted(_exports.keys())
