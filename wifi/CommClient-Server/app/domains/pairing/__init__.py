"""
app.domains.pairing — Device pairing v1 + v2 (Phase 3 Module O).

Existing implementation locations:
    app.api.routes.pair          — Original pair router (QR-based)
    app.api.routes.pairing_v2    — Pairing v2 (encrypted, multi-step)
    app.services.pairing_service — Token mint / verify
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}

# v1
got = safe_import("app.api.routes.pair", ["router", "public_router"])
if "router" in got:
    _exports["pair_router"] = got["router"]
if "public_router" in got:
    _exports["pair_public_router"] = got["public_router"]

# v2
got = safe_import("app.api.routes.pairing_v2", ["router"])
if "router" in got:
    _exports["pairing_v2_router"] = got["router"]

# Services
_exports.update(safe_import(
    "app.services.pairing_service",
    [
        "PairingService",
        "mint_pairing_token",
        "verify_pairing_token",
        "complete_pairing",
    ],
))

globals().update(_exports)
__all__ = sorted(_exports.keys())
