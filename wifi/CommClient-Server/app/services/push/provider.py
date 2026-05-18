"""
Push provider abstraction.

Concrete providers (FCM, APNs, web push) implement `send_one(token, payload)`.
The dispatcher selects a provider based on the device token's `provider` field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PushPayload:
    """Cross-provider message payload."""

    title: str
    body: str | None = None
    # Application-specific key/value pairs delivered alongside the alert
    data: dict[str, Any] = field(default_factory=dict)
    # Optional collapse / thread / category identifiers
    collapse_id: str | None = None
    sound: str | None = "default"
    badge: int | None = None
    # APNs-specific: "background" / "alert" / "voip"
    category: str | None = None
    # Set True for silent / data-only pushes
    content_available: bool = False


@dataclass
class PushResult:
    """Outcome of a single delivery attempt."""

    success: bool
    error: str | None = None
    # If True, the dispatcher should disable the token (it's invalid).
    invalid_token: bool = False
    # Provider-side message id, if any
    provider_message_id: str | None = None


class PushProvider(Protocol):
    """Provider interface — implemented by FcmProvider, ApnsProvider, etc."""

    name: str

    async def is_configured(self) -> bool:
        """Return True if the provider has the credentials it needs to send."""
        ...

    async def send_one(self, token: str, payload: PushPayload, *, extra: dict | None = None) -> PushResult:
        """Send `payload` to a single device token. Never raises."""
        ...
