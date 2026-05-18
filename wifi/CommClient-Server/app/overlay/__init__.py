"""Overlay Networks module — logical networks layered over the mesh.

Public entry points:

    from app.overlay import (
        get_overlay_manager, start_overlay, stop_overlay,
    )
"""

from app.overlay.overlay_manager import (                       # noqa: F401
    get_overlay_manager,
    start_overlay,
    stop_overlay,
)
