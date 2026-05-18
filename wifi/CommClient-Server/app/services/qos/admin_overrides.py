"""
QoS admin overrides — out-of-band controls the operator can apply mid-call.

Every override is:

  1. Validated against the live ActiveCall (404 if the call is gone).
  2. Dispatched as a Socket.IO ``qos:control`` event to the affected
     participant — call_handlers.py treats this as a hint, the client
     applies it and acknowledges via a quality_event.
  3. Mirrored into the QoS event log so it shows up next to the
     organic preset/codec switches.
  4. Audit-logged via ``app.core.audit.audit_log`` — non-repudiable trail
     of who pushed which override at what timestamp.

The communication channel is intentionally indirect (events + sockets,
not direct call_handlers calls) so we don't touch the 4,300-line handler
file. If the client doesn't honour the hint, the QoS dashboard still
sees the audit log and the absence of a corresponding ack.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.services.qos.stats_collector import qos_stats_collector

logger = get_logger(__name__)


VALID_PRESETS = frozenset({"auto", "low", "standard", "high", "hd", "4k"})
VALID_AUDIO_CODECS = frozenset({"opus", "pcma", "pcmu", "g722", "speex", "aac"})
VALID_VIDEO_CODECS = frozenset({"vp8", "vp9", "h264", "av1"})


@dataclass
class OverrideRecord:
    id: str
    timestamp: float
    actor_id: str
    call_id: str
    participant_id: str | None
    action: str
    payload: dict[str, Any]
    delivered: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QoSAdminOverrides:
    """Apply + audit admin overrides on live calls."""

    def __init__(self) -> None:
        self._history: list[OverrideRecord] = []
        self._max_history = 2000

    # ── Public command surface ─────────────────────────────────────────

    async def force_preset(
        self, call_id: str, participant_id: str, preset: str, actor_id: str,
    ) -> OverrideRecord:
        if preset not in VALID_PRESETS:
            raise ValueError(f"invalid preset '{preset}'")
        return await self._dispatch(
            call_id=call_id, participant_id=participant_id, actor_id=actor_id,
            action="force_preset", payload={"preset": preset},
            socket_event="qos:force_preset",
        )

    async def force_codec(
        self, call_id: str, participant_id: str, actor_id: str,
        codec_audio: str | None = None, codec_video: str | None = None,
    ) -> OverrideRecord:
        if not codec_audio and not codec_video:
            raise ValueError("at least one of codec_audio/codec_video required")
        if codec_audio and codec_audio.lower() not in VALID_AUDIO_CODECS:
            raise ValueError(f"invalid audio codec '{codec_audio}'")
        if codec_video and codec_video.lower() not in VALID_VIDEO_CODECS:
            raise ValueError(f"invalid video codec '{codec_video}'")
        payload = {}
        if codec_audio:
            payload["codec_audio"] = codec_audio.lower()
        if codec_video:
            payload["codec_video"] = codec_video.lower()
        return await self._dispatch(
            call_id=call_id, participant_id=participant_id, actor_id=actor_id,
            action="force_codec", payload=payload,
            socket_event="qos:force_codec",
        )

    async def toggle_simulcast(
        self, call_id: str, participant_id: str, enabled: bool, actor_id: str,
    ) -> OverrideRecord:
        return await self._dispatch(
            call_id=call_id, participant_id=participant_id, actor_id=actor_id,
            action="toggle_simulcast", payload={"enabled": bool(enabled)},
            socket_event="qos:toggle_simulcast",
        )

    async def force_turn(
        self, call_id: str, participant_id: str, actor_id: str,
    ) -> OverrideRecord:
        return await self._dispatch(
            call_id=call_id, participant_id=participant_id, actor_id=actor_id,
            action="force_turn", payload={"relay_only": True},
            socket_event="qos:force_turn",
        )

    async def chaos_inject(
        self, call_id: str, participant_id: str, actor_id: str,
        loss_pct: float | None = None, latency_ms: float | None = None,
    ) -> OverrideRecord:
        """
        Synthetic network impairment — STRICTLY for QA/testing rooms.
        The server stamps the request and forwards; the client decides
        whether to honour it (production clients should refuse unless
        running in a marked test build).
        """
        if loss_pct is None and latency_ms is None:
            raise ValueError("loss_pct or latency_ms is required")
        payload: dict[str, Any] = {}
        if loss_pct is not None:
            if loss_pct < 0 or loss_pct > 100:
                raise ValueError("loss_pct must be in [0,100]")
            payload["loss_pct"] = float(loss_pct)
        if latency_ms is not None:
            if latency_ms < 0 or latency_ms > 5000:
                raise ValueError("latency_ms must be in [0,5000]")
            payload["latency_ms"] = float(latency_ms)
        return await self._dispatch(
            call_id=call_id, participant_id=participant_id, actor_id=actor_id,
            action="chaos_inject", payload=payload,
            socket_event="qos:chaos_inject",
        )

    async def force_end(
        self, call_id: str, actor_id: str, reason: str = "admin_terminated",
    ) -> OverrideRecord:
        """Force-end a call. Goes through call_service.hangup if available."""
        delivered = False
        error: str | None = None
        try:
            from app.services.call_service import call_service
            call = call_service.get_call(call_id)
            if call is None:
                raise LookupError("call_not_found")
            await call_service.hangup(call_id, getattr(call, "initiator_id", actor_id))
            delivered = True
        except LookupError as e:
            error = str(e)
        except Exception as e:                       # pragma: no cover
            error = str(e)
            logger.warning("qos_force_end_failed", call_id=call_id, error=error)

        rec = OverrideRecord(
            id=uuid.uuid4().hex, timestamp=time.time(), actor_id=actor_id,
            call_id=call_id, participant_id=None,
            action="force_end", payload={"reason": reason},
            delivered=delivered, error=error,
        )
        self._record(rec)
        audit_log(
            "admin.qos.force_end",
            user_id=actor_id, success=delivered,
            details={"call_id": call_id, "reason": reason, "error": error},
        )
        return rec

    # ── Internal plumbing ──────────────────────────────────────────────

    async def _dispatch(
        self, *, call_id: str, participant_id: str, actor_id: str,
        action: str, payload: dict[str, Any], socket_event: str,
    ) -> OverrideRecord:
        rec = OverrideRecord(
            id=uuid.uuid4().hex, timestamp=time.time(), actor_id=actor_id,
            call_id=call_id, participant_id=participant_id,
            action=action, payload=payload,
        )

        try:
            from app.services.call_service import call_service
            call = call_service.get_call(call_id)
            if call is None:
                rec.error = "call_not_found"
            elif participant_id not in call.participants:
                rec.error = "participant_not_in_call"
        except Exception as e:                       # pragma: no cover
            rec.error = f"call_lookup_failed:{e}"

        if rec.error is None:
            try:
                from app.socket.server import emit_to_user
                envelope = {
                    "id": rec.id,
                    "action": action,
                    "call_id": call_id,
                    **payload,
                }
                await emit_to_user(socket_event, envelope, participant_id)
                rec.delivered = True
            except Exception as e:                   # pragma: no cover
                rec.error = f"emit_failed:{e}"
                logger.warning(
                    "qos_override_emit_failed",
                    action=action, call_id=call_id, error=str(e),
                )

        # Mirror into the QoS event log so the dashboard sees admin actions
        # interleaved with organic switches.
        try:
            await qos_stats_collector.ingest_event(call_id, f"admin_{action}", {
                "participant_id": participant_id,
                "actor_id": actor_id,
                "override_id": rec.id,
                **payload,
            })
        except Exception:                            # pragma: no cover
            pass

        # Audit trail
        audit_log(
            f"admin.qos.{action}",
            user_id=actor_id,
            success=rec.delivered,
            details={
                "call_id": call_id,
                "participant_id": participant_id,
                "override_id": rec.id,
                "payload": payload,
                "error": rec.error,
            },
        )
        self._record(rec)
        return rec

    def _record(self, rec: OverrideRecord) -> None:
        self._history.append(rec)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

    # ── Read API ──────────────────────────────────────────────────────

    def history(self, call_id: str | None = None, limit: int = 100) -> list[dict]:
        items = self._history
        if call_id:
            items = [r for r in items if r.call_id == call_id]
        return [r.to_dict() for r in items[-limit:]]

    def reset(self) -> None:
        self._history.clear()


qos_admin_overrides = QoSAdminOverrides()
