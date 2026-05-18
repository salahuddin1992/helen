"""
Transport network layer schemas.

Provides:
  - Transport catalog and definitions
  - Detection results with signal quality metrics
  - Bridge management and configuration
  - Capability checks per transport
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Transport Catalog ────────────────────────────────────────

class TransportDefinition(BaseModel):
    """Single transport type definition from catalog."""

    transport_id: str = Field(..., description="Unique transport identifier")
    name: str = Field(..., description="Human-readable transport name")
    category: str = Field(..., description="Transport category (e.g., ethernet, wifi, usb)")
    medium: str = Field(
        ..., description="Physical medium: wired, wireless, optical, usb, etc."
    )
    description: str = Field(..., description="Detailed description of transport")

    # Physical characteristics
    typical_bandwidth: str = Field(..., description="Typical bandwidth (e.g., '1 Gbps')")
    typical_range: str | None = Field(None, description="Typical range (e.g., '100m')")
    typical_latency: str = Field(..., description="Typical latency class (e.g., 'sub-1ms')")

    # Configuration
    detection_method: str = Field(
        ..., description="How this transport is auto-detected"
    )
    is_common: bool = Field(
        default=True, description="Whether this is a commonly available transport"
    )
    requires_hardware: bool = Field(
        default=False, description="Whether special hardware is needed"
    )

    class Config:
        from_attributes = True


class TransportListResponse(BaseModel):
    """Paginated list of transports from catalog."""

    transports: list[TransportDefinition]
    total: int
    page: int
    per_page: int
    total_pages: int


# ── Detection & Signal Quality ────────────────────────────────

class DetectedTransport(BaseModel):
    """A single detected transport on the local network."""

    transport_id: str
    name: str
    adapter_family: str = Field(..., description="OS adapter family (e.g., 'ethernet', 'wlan')")
    interface_name: str = Field(..., description="Network interface name (eth0, en0, etc.)")
    ip_address: str | None = Field(None, description="IP address of interface")
    mac_address: str | None = Field(None, description="MAC address (truncated for privacy)")
    speed: str = Field(..., description="Interface speed (e.g., '1000 Mbps')")
    mtu: int = Field(..., description="Maximum transmission unit")
    is_up: bool = Field(default=True, description="Interface is up/enabled")
    is_loopback: bool = Field(default=False, description="Is loopback interface")

    # Quality indicators
    signal_strength: float = Field(
        ..., ge=0, le=100, description="Signal strength 0-100 (100 = strongest)"
    )
    signal_quality: str = Field(
        ..., description="Signal quality label: excellent/good/fair/poor"
    )

    class Config:
        from_attributes = True


class DetectionResultResponse(BaseModel):
    """Results from a transport detection scan."""

    detected_transports: list[DetectedTransport]
    total_detected: int
    scan_timestamp: str = Field(..., description="ISO 8601 timestamp of scan")
    scan_duration_ms: float = Field(..., description="How long scan took")


# ── Signal Quality Metrics ────────────────────────────────────

class SignalQualityResponse(BaseModel):
    """Real-time signal quality measurements for a transport."""

    transport_id: str
    interface_name: str

    # Core metrics
    signal_strength: float = Field(0, ge=0, le=100, description="Signal strength 0-100")
    snr: float | None = Field(None, description="Signal-to-noise ratio in dB")
    bandwidth: float = Field(..., description="Available bandwidth in Mbps")

    # Timing metrics
    latency: float = Field(..., description="Latency in milliseconds")
    jitter: float = Field(0, description="Jitter (latency variance) in milliseconds")

    # Reliability metrics
    packet_loss: float = Field(
        0, ge=0, le=100, description="Packet loss percentage"
    )

    # Aggregate score
    quality_score: float = Field(
        ..., ge=0, le=100, description="Overall quality score 0-100"
    )
    quality_label: str = Field(
        ..., description="Label: excellent/good/fair/poor"
    )

    measured_at: str = Field(..., description="ISO 8601 timestamp of measurement")

    class Config:
        from_attributes = True


# ── Transport Statistics ──────────────────────────────────────

class TransportStatsResponse(BaseModel):
    """Aggregate transport catalog and detection statistics."""

    total_transports: int = Field(..., description="Total transports in catalog")
    detected_count: int = Field(..., description="Currently detected transports")

    by_category: dict[str, int] = Field(..., description="Count per category")
    by_medium: dict[str, int] = Field(..., description="Count per medium type")


# ── Bridge Management ──────────────────────────────────────────

class BridgeCreateRequest(BaseModel):
    """Request to create a communication bridge on a transport."""

    transport_id: str = Field(..., description="Transport to create bridge on")
    name: str = Field(..., min_length=1, max_length=256, description="Bridge display name")
    bind_port: int | None = Field(
        None, ge=1024, le=65535, description="Port to bind to (auto-select if null)"
    )

    # Protocol options
    protocol: str = Field(
        default="tcp", description="Transport protocol: tcp, udp, or both"
    )
    encryption: bool = Field(
        default=True, description="Enable encryption on bridge"
    )

    # Connection limits
    max_connections: int = Field(
        default=64, ge=1, le=1000, description="Max concurrent peers"
    )

    class Config:
        from_attributes = True


class BridgeResponse(BaseModel):
    """Status of an active communication bridge."""

    bridge_id: str
    name: str
    transport_id: str
    transport_name: str

    # Binding info
    bind_address: str
    bind_port: int

    # Status
    status: str = Field(..., description="Status: active/idle/error")
    is_encrypted: bool

    # Peer tracking
    connected_peers: list[str] = Field(default_factory=list, description="User IDs of connected peers")
    peer_count: int

    # Statistics
    bytes_sent: int = 0
    bytes_received: int = 0
    uptime_seconds: float = 0
    avg_latency_ms: float | None = None

    created_at: str = Field(..., description="ISO 8601 creation timestamp")

    class Config:
        from_attributes = True


class BridgeListResponse(BaseModel):
    """List of active bridges."""

    bridges: list[BridgeResponse]
    total: int


# ── Capabilities ──────────────────────────────────────────────

class CapabilityCheckResponse(BaseModel):
    """What communication modes are supported on a transport."""

    transport_id: str
    transport_name: str

    # Communication modes
    supports_voice: bool = Field(default=True)
    supports_video: bool = Field(default=True)
    supports_screen_share: bool = Field(default=True)
    supports_file_transfer: bool = Field(default=True)

    # Constraints
    max_participants: int = Field(default=16, ge=2, le=1000)

    # Recommendations
    recommended_codec: str | None = Field(None, description="Recommended audio codec")
    recommended_video_quality: str = Field(
        default="medium", description="Recommended video quality: low/medium/high"
    )

    notes: str | None = Field(None, description="Additional capability notes")

    class Config:
        from_attributes = True
