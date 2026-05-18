"""
Push dispatcher — fans notifications out to a user's registered devices.

Responsibilities:
  • Look up active DeviceToken rows for a user (or batch of users)
  • Pick the right provider (FCM / APNs / web) for each token
  • Send concurrently with bounded parallelism
  • Track per-token success/failure; auto-disable invalid tokens
  • Update last_used_at + failure_count fields
  • Best-effort: never raise into the caller

The dispatcher is a singleton (`push_dispatcher`) and is wired into
NotificationService so every notification create/create_bulk call also
fires a push.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.device_token import DeviceToken
from app.services.push.apns import ApnsProvider
from app.services.push.fcm import FcmProvider
from app.services.push.provider import PushPayload, PushProvider, PushResult

logger = get_logger(__name__)

# Disable a token after this many consecutive delivery failures
_FAILURE_THRESHOLD = 5
# Cap concurrent provider calls per dispatch
_MAX_CONCURRENT_SENDS = 16


class PushDispatcher:
    def __init__(self) -> None:
        self._providers: dict[str, PushProvider] = {
            "fcm": FcmProvider(),
            "apns": ApnsProvider(),
        }

    def register(self, name: str, provider: PushProvider) -> None:
        """Register a provider (used by tests to inject fakes)."""
        self._providers[name] = provider

    @property
    def providers(self) -> dict[str, PushProvider]:
        return self._providers

    # ── DB helpers ────────────────────────────────────────────

    @staticmethod
    async def _active_tokens_for_user(
        db: AsyncSession, user_id: str
    ) -> list[DeviceToken]:
        result = await db.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id,
                DeviceToken.is_active == True,  # noqa: E712
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def _active_tokens_for_users(
        db: AsyncSession, user_ids: list[str]
    ) -> list[DeviceToken]:
        if not user_ids:
            return []
        result = await db.execute(
            select(DeviceToken).where(
                DeviceToken.user_id.in_(user_ids),
                DeviceToken.is_active == True,  # noqa: E712
            )
        )
        return list(result.scalars().all())

    # ── Dispatch ──────────────────────────────────────────────

    async def dispatch(
        self,
        db: AsyncSession,
        user_id: str,
        payload: PushPayload,
    ) -> dict:
        """Send a push to all of one user's devices. Returns summary."""
        tokens = await self._active_tokens_for_user(db, user_id)
        return await self._dispatch_tokens(db, tokens, payload)

    async def dispatch_bulk(
        self,
        db: AsyncSession,
        user_ids: list[str],
        payload: PushPayload,
    ) -> dict:
        """Send a push to all devices of multiple users."""
        tokens = await self._active_tokens_for_users(db, user_ids)
        return await self._dispatch_tokens(db, tokens, payload)

    async def _dispatch_tokens(
        self,
        db: AsyncSession,
        tokens: list[DeviceToken],
        payload: PushPayload,
    ) -> dict:
        if not tokens:
            return {"sent": 0, "failed": 0, "disabled": 0, "skipped": 0}

        sem = asyncio.Semaphore(_MAX_CONCURRENT_SENDS)
        results: list[tuple[DeviceToken, PushResult]] = []

        async def _send(tok: DeviceToken) -> None:
            provider = self._providers.get(tok.provider)
            if provider is None:
                results.append((tok, PushResult(success=False, error="unknown_provider")))
                return
            if not await provider.is_configured():
                results.append((tok, PushResult(success=False, error="provider_not_configured")))
                return
            extra: dict = {}
            if tok.bundle_id:
                extra["bundle_id"] = tok.bundle_id
            if tok.extra_json:
                try:
                    extra.update(json.loads(tok.extra_json))
                except (ValueError, TypeError):
                    pass
                except json.JSONDecodeError:
                    pass
            async with sem:
                try:
                    res = await provider.send_one(tok.token, payload, extra=extra)
                except Exception as e:  # never propagate
                    res = PushResult(success=False, error=f"provider_exception:{e}")
            results.append((tok, res))

        # ``return_exceptions=True`` so a single device's send failure
        # doesn't cancel the rest of the fan-out. _send() already
        # records exceptions into ``results`` itself; this guards
        # against any unforeseen escape (e.g. a CancelledError from
        # the semaphore that would otherwise tear down the gather).
        await asyncio.gather(
            *(_send(t) for t in tokens), return_exceptions=True,
        )

        # Apply DB updates in one pass
        sent = failed = disabled = skipped = 0
        now = datetime.now(timezone.utc)
        _disabled_user_ids: set[str] = set()
        for tok, res in results:
            if res.error == "provider_not_configured":
                skipped += 1
                continue
            if res.success:
                sent += 1
                tok.last_used_at = now
                tok.failure_count = 0
                tok.last_error = None
            else:
                failed += 1
                tok.failure_count = (tok.failure_count or 0) + 1
                tok.last_error = (res.error or "unknown")[:256]
                if res.invalid_token or tok.failure_count >= _FAILURE_THRESHOLD:
                    tok.is_active = False
                    disabled += 1
                    _disabled_user_ids.add(tok.user_id)
        try:
            await db.commit()
        except Exception as e:
            logger.warning("push_dispatch_commit_failed", error=str(e))
            await db.rollback()

        # DLQ: if a dispatch ended with *every* token failing (not just one
        # noisy device) we capture the payload so an admin can replay it
        # after re-registering valid tokens. ``sent == 0 and failed > 0``
        # cleanly distinguishes this from the "nobody had tokens" case.
        if sent == 0 and failed > 0:
            try:
                from app.services.dead_letter_service import record as _dlq_record
                user_ids = sorted({t.user_id for t in tokens})
                await _dlq_record(
                    kind="push",
                    reason="push_dispatch_all_failed",
                    error=f"tokens={len(tokens)} failed={failed} disabled={disabled}",
                    payload={
                        "user_ids": user_ids,
                        "title": payload.title,
                        "body": payload.body,
                        "data": payload.data or {},
                        "disabled_user_ids": sorted(_disabled_user_ids),
                    },
                )
            except Exception:
                pass

        logger.info(
            "push_dispatch_done",
            tokens=len(tokens),
            sent=sent,
            failed=failed,
            disabled=disabled,
            skipped=skipped,
        )
        return {"sent": sent, "failed": failed, "disabled": disabled, "skipped": skipped}


push_dispatcher = PushDispatcher()
