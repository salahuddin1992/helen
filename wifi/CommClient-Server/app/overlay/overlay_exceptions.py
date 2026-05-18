"""Custom exception hierarchy for the overlay package."""

from __future__ import annotations


class OverlayError(Exception):
    """Base class for every overlay exception."""


class OverlayNotFoundError(OverlayError):
    """Referenced overlay name has no registry entry."""


class OverlayNodeError(OverlayError):
    """Node operation (add/remove/lookup) failed."""


class OverlayLinkError(OverlayError):
    """Link operation failed (loop detected, missing endpoint, etc.)."""


class OverlayRouteError(OverlayError):
    """Route resolution could not find a path."""


class OverlaySessionError(OverlayError):
    """Session operation failed (duplicate id, expired, etc.)."""


class OverlayConfigError(OverlayError):
    """Overlay-package configuration is invalid."""
