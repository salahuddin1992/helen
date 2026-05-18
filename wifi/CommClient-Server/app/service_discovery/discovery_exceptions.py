"""Exception hierarchy for the service-discovery package."""

from __future__ import annotations


class ServiceDiscoveryError(Exception):
    """Base class for every service-discovery exception."""


class ServiceNotFoundError(ServiceDiscoveryError):
    """No registered service satisfies the lookup criteria."""


class ServiceRegistrationError(ServiceDiscoveryError):
    """Registration failed (signature invalid, duplicate, schema)."""


class StaleEntryError(ServiceDiscoveryError):
    """Looked up record is older than its TTL — caller should retry."""


class SignatureError(ServiceDiscoveryError):
    """HMAC signature on a registration / heartbeat is invalid.
    Distinct from generic registration errors so callers can ban the
    source quickly."""


class RegionMismatchError(ServiceDiscoveryError):
    """Caller asked for a region, none available without crossing cluster."""


class CapacityExceededError(ServiceDiscoveryError):
    """No service has remaining capacity to take the request."""


class FederationLookupError(ServiceDiscoveryError):
    """Cross-cluster lookup failed."""
