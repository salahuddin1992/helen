"""
app.domains.files — Uploads (single + resumable), acceptance tracking, file drop.

Existing implementation locations:
    app.api.routes.files                — /api/files/* router
    app.api.routes.files_resumable      — chunked uploads
    app.api.routes.file_acceptance      — per-recipient ack tracking
    app.api.routes.file_drop            — anonymous LAN drop
    app.api.routes.media_gallery        — gallery
    app.api.routes.group_file_offers    — group offers
    app.services.file_service           — storage orchestration
    app.services.file_acceptance_service — acceptance state machine
    app.services.file_drop_service      — drop session lifecycle
    app.models.file / file_acceptance / file_drop / upload_session / group_file_offer
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}


def _add_router(modpath: str, alias: str) -> None:
    got = safe_import(modpath, ["router"])
    if "router" in got:
        _exports[alias] = got["router"]


_add_router("app.api.routes.files",             "files_router")
_add_router("app.api.routes.files_resumable",   "files_resumable_router")
_add_router("app.api.routes.file_acceptance",   "file_acceptance_router")
_add_router("app.api.routes.file_drop",         "file_drop_router")
_add_router("app.api.routes.media_gallery",     "media_gallery_router")
_add_router("app.api.routes.group_file_offers", "group_file_offers_router")

# Sub-router on file_acceptance
_exports.update(safe_import(
    "app.api.routes.file_acceptance",
    ["inbox_router"],
))
if "inbox_router" in _exports:
    _exports["file_acceptance_inbox_router"] = _exports.pop("inbox_router")

# Services
_exports.update(safe_import(
    "app.services.file_service",
    [
        "FileService",
        "store_upload",
        "stream_download",
        "delete_file",
    ],
))
_exports.update(safe_import(
    "app.services.file_acceptance_service",
    [
        "FileAcceptanceService",
        "record_accept",
        "record_reject",
        "list_pending_for_user",
    ],
))
_exports.update(safe_import(
    "app.services.file_drop_service",
    ["FileDropService", "create_drop_session", "claim_drop"],
))

# Models
_exports.update(safe_import("app.models.file", ["File", "FileVisibility"]))
_exports.update(safe_import("app.models.file_acceptance", ["FileAcceptance", "AcceptanceStatus"]))
_exports.update(safe_import("app.models.file_drop", ["FileDrop"]))
_exports.update(safe_import("app.models.upload_session", ["UploadSession"]))
_exports.update(safe_import("app.models.group_file_offer", ["GroupFileOffer"]))
_exports.update(safe_import("app.models.media_gallery", ["MediaGallery", "MediaItem"]))

globals().update(_exports)
__all__ = sorted(_exports.keys())
