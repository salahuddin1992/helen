"""
app.domains.messaging — Channels, messages, drafts, mentions, reactions.

Existing implementation locations:
    app.api.routes.messages            — /api/messages/* + search + msg + channel sub-routers
    app.api.routes.channels            — /api/channels/* router
    app.api.routes.channel_join        — /api/channels/{id}/join
    app.api.routes.channel_slow_mode   — slow-mode settings
    app.api.routes.channel_message_ttl — TTL/ephemeral
    app.api.routes.channel_categories  — category grouping
    app.api.routes.drafts              — per-channel drafts
    app.api.routes.custom_emoji        — workspace emoji
    app.services.channel_service       — channel CRUD + membership
    app.socket.chat_handlers           — Socket.IO message events
    app.models.message / channel       — ORM models
"""

from __future__ import annotations

from app.domains._safe_import import safe_import, safe_module

_exports: dict = {}

# HTTP routers (aliases prevent collision when re-exporting many "router" symbols)
def _add_router(modpath: str, alias: str) -> None:
    got = safe_import(modpath, ["router"])
    if "router" in got:
        _exports[alias] = got["router"]


_add_router("app.api.routes.messages",            "messages_router")
_add_router("app.api.routes.channels",            "channels_router")
_add_router("app.api.routes.channel_join",        "channel_join_router")
_add_router("app.api.routes.channel_slow_mode",   "channel_slow_mode_router")
_add_router("app.api.routes.channel_message_ttl", "channel_ttl_router")
_add_router("app.api.routes.channel_categories",  "channel_categories_router")
_add_router("app.api.routes.drafts",              "drafts_router")
_add_router("app.api.routes.custom_emoji",        "custom_emoji_router")
_add_router("app.api.routes.scheduled_messages",  "scheduled_messages_router")
_add_router("app.api.routes.saved_messages",      "saved_messages_router")

# Sub-routers exported by messages module
_exports.update(safe_import(
    "app.api.routes.messages",
    ["search_router", "msg_router", "channel_router"],
))

# Services
_exports.update(safe_import(
    "app.services.channel_service",
    [
        "ChannelService",
        "create_channel",
        "list_channels_for_user",
        "add_member",
        "remove_member",
    ],
))

# Models
_exports.update(safe_import("app.models.message", ["Message", "MessageType"]))
_exports.update(safe_import("app.models.channel", ["Channel", "ChannelType"]))
_exports.update(safe_import("app.models.message_draft", ["MessageDraft"]))
_exports.update(safe_import("app.models.message_edit_history", ["MessageEditHistory"]))
_exports.update(safe_import("app.models.channel_category", ["ChannelCategory"]))
_exports.update(safe_import("app.models.scheduled_message", ["ScheduledMessage"]))
_exports.update(safe_import("app.models.saved_message", ["SavedMessage"]))

# Socket.IO handlers (whole module re-export)
_chat = safe_module("app.socket.chat_handlers")
if _chat is not None:
    _exports["chat_handlers"] = _chat

globals().update(_exports)
__all__ = sorted(_exports.keys())
