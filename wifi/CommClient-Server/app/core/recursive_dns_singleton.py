"""Process-wide singleton for the Helen-Router recursive DNS server.

Helen-Server's admin endpoints query the Pi-hole-style filtering DNS
that lives in Helen-Router's package. To keep both packages decoupled
on disk, the server stores a reference here at startup time and the
admin route reads it back through ``get_recursive_dns()``.
"""

from __future__ import annotations

from typing import Any, Optional


_server: Optional[Any] = None


def set_recursive_dns(srv: Any) -> None:
    global _server
    _server = srv


def get_recursive_dns() -> Optional[Any]:
    return _server


def clear_recursive_dns() -> None:
    global _server
    _server = None


__all__ = ["set_recursive_dns", "get_recursive_dns", "clear_recursive_dns"]
