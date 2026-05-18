"""
TURN service request/response schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class TURNCredentialsRequest(BaseModel):
    """Request temporary TURN credentials."""

    username: str = Field(..., min_length=1, max_length=256, description="User identifier")
    ttl_seconds: int = Field(3600, ge=60, le=86400, description="Credential lifetime in seconds")


class TURNCredentialsResponse(BaseModel):
    """Temporary TURN credentials response."""

    username: str = Field(..., description="Ephemeral TURN username (timestamp:original_username)")
    password: str = Field(..., description="HMAC-SHA1 derived password")
    ttl: int = Field(..., description="Seconds until expiry")
    realm: str = Field(..., description="Authentication realm")


class TURNAllocationRequest(BaseModel):
    """Request TURN allocation."""

    username: str = Field(..., description="Ephemeral TURN username from credentials")
    password: str = Field(..., description="TURN password from credentials")
    client_ip: str = Field(..., description="Client's IP address")
    client_port: int = Field(..., ge=1, le=65535, description="Client's port")
    transport: str = Field("udp", pattern=r"^(udp|tcp)$", description="Relay transport")
    lifetime: int = Field(600, ge=60, le=3600, description="Allocation lifetime in seconds")


class TURNAllocationResponse(BaseModel):
    """TURN allocation response."""

    allocation_id: str = Field(..., description="Allocation identifier")
    relay_ip: str = Field(..., description="Relay server IP address")
    relay_port: int = Field(..., ge=1, le=65535, description="Relay server port")
    username: str = Field(..., description="Authenticated username")
    lifetime: int = Field(..., description="Allocation lifetime in seconds")
    transport: str = Field(..., description="Relay transport (udp/tcp)")


class TURNAllocationDetail(BaseModel):
    """Detailed allocation information."""

    allocation_id: str
    username: str
    relay_address: str
    client_address: str
    transport: str
    lifetime_seconds: int
    seconds_remaining: float
    permissions_count: int
    channels_count: int
    bytes_relayed: int
    packets_relayed: int


class TURNStats(BaseModel):
    """TURN service statistics."""

    timestamp: str = Field(..., description="ISO 8601 timestamp")
    realm: str = Field(..., description="Authentication realm")
    active_allocations: int = Field(..., description="Currently active allocations")
    allocations: list[TURNAllocationDetail] = Field(
        default_factory=list, description="Detailed allocation list"
    )
    total_allocations_created: int = Field(..., description="Total allocations created since startup")
    total_bytes_relayed: int = Field(..., description="Total bytes relayed since startup")
    total_packets_relayed: int = Field(..., description="Total packets relayed since startup")


class PermissionRequest(BaseModel):
    """Request to add/refresh permission."""

    peer_ip: str = Field(..., description="Peer IP address")
    peer_port: int = Field(..., ge=1, le=65535, description="Peer port")
    lifetime: int = Field(300, ge=60, le=3600, description="Permission lifetime in seconds")


class ChannelBindRequest(BaseModel):
    """Request to bind channel number."""

    channel_number: int = Field(..., ge=0x4000, le=0x7FFF, description="Channel number (0x4000-0x7FFF)")
    peer_ip: str = Field(..., description="Peer IP address")
    peer_port: int = Field(..., ge=1, le=65535, description="Peer port")
