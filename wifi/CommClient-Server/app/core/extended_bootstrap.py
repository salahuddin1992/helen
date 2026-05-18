"""
Extended bootstrap — LAN server hardening entry point (Task #4).

This module is the single place that wires all new LAN-server
extensions (persistent secrets, LAN CORS, Socket.IO origin patch, SFU
auto-launcher) WITHOUT modifying the existing `app.main` file.

Integration model
-----------------
Two layers:

  1. Module-import side effects
     ---------------------------
     `app.core.extended_bootstrap` MUST be imported BEFORE
     `app.core.config.get_settings()` is first evaluated — because that's
     when `Settings.JWT_SECRET`'s `default_factory` runs, and the secret
     must be in `os.environ` by then.

     The clean way to guarantee that ordering is to import this module
     from `app/__init__.py`, which runs before anything under `app.*`
     imports config. See `_ensure_early_import()` below — it is the
     body of that hook.

  2. Lifespan augmentation
     ---------------------
     `apply_extended_lifespan(app)` wraps the existing FastAPI `lifespan`
     context manager with an outer layer that:
        - starts the SFU worker
        - attaches LAN CORS middleware
        - patches the Socket.IO origin list

     The wrapper preserves the original lifespan entirely, so all the
     existing background loops and shutdown behaviour stay exactly as
     authored.

Nothing in `app.main` needs to change to benefit from this module —
the launcher (`run.py` or Electron spawn) only needs one extra line:

    from app.core import extended_bootstrap  # noqa: F401
    extended_bootstrap.apply_extended_lifespan(app)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: early-import side effects
# ─────────────────────────────────────────────────────────────────────────────
#
# IMPORTANT: We do persistent-secrets loading at IMPORT time so that any
# downstream import of `app.core.config` picks up JWT_SECRET from the
# environment. Anything else (logging, SFU, CORS) is deferred to lifespan.


def _ensure_early_import() -> None:
    try:
        from app.core.persistent_secrets import ensure_persistent_secrets_loaded
        ensure_persistent_secrets_loaded()
    except Exception as exc:
        # We cannot rely on logging yet — fall back to stderr.
        import sys
        print(
            f"[extended_bootstrap] persistent_secrets failed: {exc}",
            file=sys.stderr,
        )


_ensure_early_import()


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: lifespan augmentation
# ─────────────────────────────────────────────────────────────────────────────


_applied = False


def apply_extended_lifespan(app: Any) -> None:
    """
    Wrap `app.router.lifespan_context` with the LAN-server extensions.

    Idempotent. Safe to call multiple times — second call is a no-op.
    """
    global _applied
    if _applied:
        return

    from app.core.lan_cors import attach_lan_cors
    from app.core.logging import get_logger

    logger = get_logger(__name__)

    # Attach LAN CORS BEFORE the app starts — Starlette builds its
    # middleware stack on first request and add_middleware is rejected
    # once that happens. Doing it here (before we wrap lifespan) is the
    # only safe moment.
    try:
        attach_lan_cors(app)
        logger.info("extended_bootstrap_cors_attached")
    except Exception as exc:
        logger.error("extended_bootstrap_cors_failed", error=str(exc))

    # Apply runtime overrides (e.g. admin-edited SERVER_NAME) as early as
    # possible so the first discovery broadcast uses the persisted value.
    try:
        from app.services.server_config_service import server_config_service
        server_config_service.load_and_apply()
    except Exception as exc:
        logger.error("extended_bootstrap_server_config_failed", error=str(exc))

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _wrapped_lifespan(app_arg: Any):
        # ── pre-startup extensions ─────────────────────────────────

        # Socket.IO origin patch can only run AFTER `app.socket.server`
        # has been imported. `app.main` imports the socket package as
        # part of `create_combined_app()`, so by the time we get here
        # `sio` already exists.
        try:
            from app.socket.lan_origin_patch import patch_socketio_cors
            patch_socketio_cors()
        except Exception as exc:
            logger.error("extended_bootstrap_socketio_patch_failed", error=str(exc))

        sfu_started = False
        try:
            from app.services.sfu_launcher import sfu_launcher
            await sfu_launcher.start()
            sfu_started = sfu_launcher.is_enabled()
            logger.info(
                "extended_bootstrap_sfu_started",
                enabled=sfu_launcher.is_enabled(),
                snapshot=sfu_launcher.snapshot(),
            )
        except Exception as exc:
            logger.error("extended_bootstrap_sfu_start_failed", error=str(exc))

        # Start the multi-transport coordinator (TCP fallback + health watchdog).
        transport_started = False
        try:
            from app.services.transport_coordinator import transport_coordinator
            await transport_coordinator.start()
            transport_started = True
            logger.info(
                "extended_bootstrap_transport_started",
                snapshot=transport_coordinator.get_snapshot()["summary"],
            )
        except Exception as exc:
            logger.error("extended_bootstrap_transport_start_failed", error=str(exc))

        # Auto-start any ingest sources flagged as auto_start=True.
        # Runs *after* SFU so producers can be wired in a follow-up patch.
        ingest_autostarted: list[str] = []
        try:
            from app.services.ingest_service import ingest_service
            ingest_autostarted = await ingest_service.autostart()
            logger.info(
                "extended_bootstrap_ingest_autostart",
                started=ingest_autostarted,
                count=len(ingest_autostarted),
            )
        except Exception as exc:
            logger.error("extended_bootstrap_ingest_autostart_failed", error=str(exc))

        # Update-mirror HTTP routes — mount even on non-leaders so
        # clients can hit any LAN node and receive the same manifest.
        try:
            from app.api.routes.update import router as _update_router
            # Guard against double-mount when lifespan is wrapped twice.
            if not any(
                getattr(r, "path", "").startswith("/api/updates") for r in app_arg.routes
            ):
                app_arg.include_router(_update_router)
                logger.info("extended_bootstrap_update_routes_mounted")
        except Exception as exc:
            logger.error("extended_bootstrap_update_routes_failed", error=str(exc))

        # Leader-only background mirror refresher.
        update_task: Any = None
        try:
            import asyncio as _asyncio
            from app.services.update_service import (
                update_service as _update_service,
                UpdateServiceConfig,
            )
            cfg = UpdateServiceConfig.from_env()
            if cfg.upstream_url:
                try:
                    from app.main import run_supervised_as_leader  # type: ignore
                    update_task = _asyncio.create_task(
                        run_supervised_as_leader(
                            "update_mirror",
                            _update_service.run_forever,
                        )
                    )
                except Exception:
                    # Fallback: run unsupervised (single-node deployments).
                    update_task = _asyncio.create_task(_update_service.run_forever())
                logger.info("extended_bootstrap_update_mirror_started",
                            upstream=cfg.upstream_url)
            else:
                logger.info("extended_bootstrap_update_mirror_skipped_no_upstream")
        except Exception as exc:
            logger.error("extended_bootstrap_update_mirror_failed", error=str(exc))

        # ── hand control to the original lifespan ──────────────────
        try:
            async with original_lifespan(app_arg):
                # Log a one-shot banner with connection instructions.
                try:
                    from app.services.lan_ice_helper import all_announce_ips, primary_lan_ip
                    from app.core.config import get_settings
                    port = get_settings().PORT
                    logger.info(
                        "lan_server_ready_banner",
                        primary_ip=primary_lan_ip(),
                        all_ips=all_announce_ips(),
                        port=port,
                        connect_url=f"http://{primary_lan_ip()}:{port}",
                    )
                except Exception:
                    pass
                yield
        finally:
            # ── shutdown extensions ────────────────────────────────
            try:
                from app.services.ingest_service import ingest_service
                await ingest_service.shutdown_all()
                logger.info("extended_bootstrap_ingest_stopped")
            except Exception as exc:
                logger.error("extended_bootstrap_ingest_stop_failed", error=str(exc))

            if transport_started:
                try:
                    from app.services.transport_coordinator import transport_coordinator
                    await transport_coordinator.stop()
                    logger.info("extended_bootstrap_transport_stopped")
                except Exception as exc:
                    logger.error("extended_bootstrap_transport_stop_failed", error=str(exc))

            if sfu_started:
                try:
                    from app.services.sfu_launcher import sfu_launcher
                    await sfu_launcher.stop()
                    logger.info("extended_bootstrap_sfu_stopped")
                except Exception as exc:
                    logger.error("extended_bootstrap_sfu_stop_failed", error=str(exc))

            if update_task is not None:
                try:
                    from app.services.update_service import update_service as _update_service
                    _update_service.stop()
                    await update_task
                    logger.info("extended_bootstrap_update_mirror_stopped")
                except Exception as exc:
                    logger.error("extended_bootstrap_update_mirror_stop_failed",
                                 error=str(exc))

    app.router.lifespan_context = _wrapped_lifespan
    _applied = True


__all__ = ["apply_extended_lifespan"]
