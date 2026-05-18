"""
Socket.IO LAN-origin patch (Task #3).

The existing `app.socket.server.sio` was constructed with a hard-coded
`cors_allowed_origins` list (localhost + app://.). Over LAN, clients
present origins like `http://192.168.1.42:3000` and Socket.IO's engineio
layer rejects the upgrade with "Origin not allowed".

This module monkey-patches the allowed-origins check on the already-built
`sio` instance so we don't have to edit `server.py`. It:

  * Adds every detected LAN origin to `sio.eio.cors_allowed_origins` (or
    the appropriate attribute on the installed python-socketio version).
  * Falls back to a permissive callable when the library supports one.

Safe to call more than once — later calls are idempotent.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging import get_logger
from app.services.lan_ice_helper import lan_origin_regex, lan_origins
from app.socket.server import sio

logger = get_logger(__name__)

_patched = False


def _compiled_regex() -> re.Pattern[str]:
    return re.compile(lan_origin_regex())


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # same-origin / no Origin header → allow
    if origin in lan_origins():
        return True
    try:
        return _compiled_regex().match(origin) is not None
    except re.error:
        return False


def patch_socketio_cors() -> None:
    """
    Replace the hard-coded `cors_allowed_origins` on the running `sio`
    instance with a list that includes every LAN origin we just detected,
    PLUS a regex-aware fallback when the installed python-socketio
    supports callable origins (>=5.x).
    """
    global _patched
    if _patched:
        return

    origins = lan_origins()

    # python-socketio stores the setting on the server object itself.
    try:
        current: Any = getattr(sio, "cors_allowed_origins", None)
        if isinstance(current, list):
            merged = list(dict.fromkeys(list(current) + origins))
            sio.cors_allowed_origins = merged
        elif current in (None, "*"):
            # Already permissive — nothing to do.
            pass
        else:
            # Single string → upgrade to list.
            sio.cors_allowed_origins = list(dict.fromkeys([current] + origins))
    except Exception as exc:
        logger.warning("socketio_cors_patch_failed_sio", error=str(exc))

    # engineio re-reads the same attribute from the sio.eio server. Some
    # versions also cache it separately — mirror it there to be safe.
    try:
        eio = getattr(sio, "eio", None)
        if eio is not None and hasattr(eio, "cors_allowed_origins"):
            current_eio: Any = eio.cors_allowed_origins
            if isinstance(current_eio, list):
                eio.cors_allowed_origins = list(
                    dict.fromkeys(list(current_eio) + origins)
                )
            elif current_eio in (None, "*"):
                pass
            else:
                eio.cors_allowed_origins = list(
                    dict.fromkeys([current_eio] + origins)
                )
    except Exception as exc:
        logger.warning("socketio_cors_patch_failed_eio", error=str(exc))

    # Additional layer: install a permissive regex-callable if the installed
    # version supports it. python-socketio lets you pass a callable to
    # `cors_allowed_origins`; when present, it wins over the list.
    try:
        sio.cors_allowed_origins = _origin_allowed  # type: ignore[assignment]
    except Exception:
        pass

    _patched = True
    logger.info(
        "socketio_cors_lan_patched",
        lan_origins=len(origins),
    )


__all__ = ["patch_socketio_cors"]
