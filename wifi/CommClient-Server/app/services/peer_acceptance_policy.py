"""
Peer acceptance policy — central decision point for what happens to
a verified peer.

The verification step (HMAC + cluster_id + nonce + timestamp +
version + capabilities) is the same in EVERY mode. The policy only
decides what state to put the peer into AFTER verification:

    auto_accept       → AUTO_ACCEPTED → PROVISIONING
    manual_approval   → WAITING_MANUAL_APPROVAL → admin → APPROVED
    pending_approval  → PENDING_APPROVAL → admin → APPROVED
    human_selection   → AWAITING_HUMAN_SELECTION → admin → APPROVED

Failed verification → REJECTED in every mode. There is no policy
override that bypasses verification — manual_approval doesn't mean
"trust without checking", it means "verified but not yet trusted".

The mode is set via ``COMMCLIENT_PEER_ACCEPTANCE_MODE`` and validated
at startup. Operators can also hard-disable specific modes via the
``COMMCLIENT_ALLOW_*`` flags as a defense against accidental config
drift (e.g. lab cluster shouldn't accidentally run in auto_accept).

Hot reload: the mode is read from settings on each policy call so a
config change takes effect without restart. Production deployments
should still treat changes as a planned operation — rotating from
manual_approval to auto_accept is a security-sensitive action.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.server_node import (
    PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_WAITING_MANUAL_APPROVAL,
    PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_AWAITING_HUMAN,
)

logger = get_logger(__name__)


class PeerAcceptanceMode(str, Enum):
    AUTO_ACCEPT = "auto_accept"
    MANUAL_APPROVAL = "manual_approval"
    PENDING_APPROVAL = "pending_approval"
    HUMAN_SELECTION = "human_selection"


# Map mode → state to put a verified candidate into. Centralized so a
# new mode can't accidentally land a peer in the wrong state.
MODE_TO_VERIFIED_STATE = {
    PeerAcceptanceMode.AUTO_ACCEPT:      PEER_STATE_AUTO_ACCEPTED,
    PeerAcceptanceMode.MANUAL_APPROVAL:  PEER_STATE_WAITING_MANUAL_APPROVAL,
    PeerAcceptanceMode.PENDING_APPROVAL: PEER_STATE_PENDING_APPROVAL,
    PeerAcceptanceMode.HUMAN_SELECTION:  PEER_STATE_AWAITING_HUMAN,
}


class InvalidModeError(Exception):
    """Raised when COMMCLIENT_PEER_ACCEPTANCE_MODE is unknown or
    explicitly disabled via COMMCLIENT_ALLOW_* flags."""


class PeerAcceptancePolicy:
    """Stateless policy. Singleton via ``get_policy()``."""

    def get_mode(self) -> PeerAcceptanceMode:
        """Resolve the current acceptance mode. Raises
        InvalidModeError if the configured mode is unknown or
        disabled.

        Live override: when the runtime sync_policy is paused (admin
        flipped the kill-switch), force MANUAL_APPROVAL so newly
        discovered peers park instead of auto-joining. Existing
        peers are unaffected — pause only gates *new* arrivals.
        """
        # Runtime kill-switch wins over static config.
        try:
            from app.services.sync_policy import get_sync_policy
            if get_sync_policy().paused:
                return PeerAcceptanceMode.MANUAL_APPROVAL
        except Exception:
            pass

        settings = get_settings()
        raw = (settings.COMMCLIENT_PEER_ACCEPTANCE_MODE or "").strip().lower()
        if not raw:
            # Unconfigured → default to manual_approval, the safest
            # mode that still allows operations.
            raw = "manual_approval"

        try:
            mode = PeerAcceptanceMode(raw)
        except ValueError:
            raise InvalidModeError(
                f"Unknown COMMCLIENT_PEER_ACCEPTANCE_MODE={raw!r}. "
                f"Valid: {', '.join(m.value for m in PeerAcceptanceMode)}."
            )

        # Enforce the per-mode allow flags.
        allowed_map = {
            PeerAcceptanceMode.AUTO_ACCEPT:      settings.COMMCLIENT_ALLOW_AUTO_ACCEPT,
            PeerAcceptanceMode.MANUAL_APPROVAL:  settings.COMMCLIENT_ALLOW_MANUAL_APPROVAL,
            PeerAcceptanceMode.PENDING_APPROVAL: settings.COMMCLIENT_ALLOW_PENDING_APPROVAL,
            PeerAcceptanceMode.HUMAN_SELECTION:  settings.COMMCLIENT_ALLOW_HUMAN_SELECTION,
        }
        if not allowed_map.get(mode, False):
            raise InvalidModeError(
                f"Mode {mode.value!r} is set but disabled via "
                f"COMMCLIENT_ALLOW_{mode.value.upper()}=false."
            )
        return mode

    def state_for_verified_peer(self, mode: PeerAcceptanceMode | None = None) -> str:
        """Return the ServerNode.approval_status string a verified peer
        should land in for the active (or supplied) mode."""
        mode = mode or self.get_mode()
        return MODE_TO_VERIFIED_STATE[mode]

    def should_auto_accept(self) -> bool:
        return self.get_mode() == PeerAcceptanceMode.AUTO_ACCEPT

    def requires_manual_approval(self) -> bool:
        return self.get_mode() == PeerAcceptanceMode.MANUAL_APPROVAL

    def should_place_pending(self) -> bool:
        return self.get_mode() == PeerAcceptanceMode.PENDING_APPROVAL

    def should_require_human_selection(self) -> bool:
        return self.get_mode() == PeerAcceptanceMode.HUMAN_SELECTION

    def validate_mode_config(self) -> None:
        """Run at startup; raises InvalidModeError on misconfig."""
        mode = self.get_mode()
        logger.info(
            "peer_acceptance_policy_active",
            mode=mode.value,
            cluster_id=get_settings().COMMCLIENT_CLUSTER_ID,
        )


# ── Module-level singleton ──────────────────────────────────────────

_policy: PeerAcceptancePolicy | None = None


def get_policy() -> PeerAcceptancePolicy:
    global _policy
    if _policy is None:
        _policy = PeerAcceptancePolicy()
    return _policy
