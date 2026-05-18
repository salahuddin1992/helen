"""
Transport type definitions and enums.
Comprehensive type system for network transport abstraction.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TransportMedium(str, Enum):
    """Physical or virtual medium used by transport."""
    WIRED = "wired"
    WIRELESS = "wireless"
    OPTICAL = "optical"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"


class LatencyClass(str, Enum):
    """Latency classification for network quality."""
    ULTRA_LOW = "ultra_low"  # < 5ms
    LOW = "low"              # 5-50ms
    MEDIUM = "medium"        # 50-150ms
    HIGH = "high"            # 150-500ms
    VERY_HIGH = "very_high"  # > 500ms


class SecurityLevel(str, Enum):
    """Security classification of transport."""
    NONE = "none"
    BASIC = "basic"
    MEDIUM = "medium"
    HIGH = "high"
    MILITARY = "military"


class DetectionMethod(str, Enum):
    """Method used to detect this transport."""
    INTERFACE_SCAN = "interface_scan"
    DRIVER_CHECK = "driver_check"
    PORT_SCAN = "port_scan"
    SERVICE_DISCOVERY = "service_discovery"
    HARDWARE_PROBE = "hardware_probe"
    MANUAL = "manual"
    API_QUERY = "api_query"


class TransportStatus(str, Enum):
    """Operational status of transport."""
    AVAILABLE = "available"
    ACTIVE = "active"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class TransportCategory(str, Enum):
    """46 transport categories matching transport_catalog.json."""
    # ── Original 30 ──────────────────────────────────────
    ETHERNET = "ethernet"
    WIFI = "wifi"
    FIBER = "fiber"
    WIRELESS_BRIDGE = "wireless_bridge"
    CELLULAR_PRIVATE = "cellular_private"
    RADIO = "radio"
    MESH = "mesh"
    INDUSTRIAL = "industrial"
    SERIAL_BUS = "serial_bus"
    POWERLINE = "powerline"
    OPTICAL_LINK = "optical_link"
    HIGH_PERFORMANCE = "high_performance"
    OVERLAY_TUNNEL = "overlay_tunnel"
    AV_NETWORK = "av_network"
    IOT_SENSOR = "iot_sensor"
    LEGACY = "legacy"
    STORAGE_NETWORK = "storage_network"
    MANAGEMENT = "management"
    SATELLITE_AEROSPACE = "satellite_aerospace"
    TACTICAL_EMERGENCY = "tactical_emergency"
    TOPOLOGY = "topology"
    DATACENTER_FABRIC = "datacenter_fabric"
    SECURITY_ISOLATED = "security_isolated"
    BUILDING_CAMPUS = "building_campus"
    SCADA_UTILITY = "scada_utility"
    TRANSPORT_VEHICLE = "transport_vehicle"
    SERVICE_OVERLAY = "service_overlay"
    WAN_PRIVATE = "wan_private"
    SPECIALTY_VERTICAL = "specialty_vertical"
    TIME_SENSITIVE = "time_sensitive"
    # ── New 16 categories ────────────────────────────────
    MILITARY_DEFENSE = "military_defense"
    AUTOMOTIVE = "automotive"
    MEDICAL = "medical"
    MARITIME_UNDERWATER = "maritime_underwater"
    ENERGY_GRID = "energy_grid"
    DEEP_SPACE = "deep_space"
    QUANTUM_EXPERIMENTAL = "quantum_experimental"
    AVIATION = "aviation"
    MINING_UNDERGROUND = "mining_underground"
    RAILWAY = "railway"
    BROADCAST_MEDIA = "broadcast_media"
    FINANCIAL_TRADING = "financial_trading"
    NUCLEAR = "nuclear"
    EMERGENCY_PUBLIC_SAFETY = "emergency_public_safety"
    ACOUSTIC = "acoustic"
    DRONE_UAV = "drone_uav"
    # ── Always last ──────────────────────────────────────
    CUSTOM = "custom"


class TransportDefinition(BaseModel):
    """Complete definition of a network transport type."""
    id: str = Field(..., description="Unique transport identifier")
    name: str = Field(..., description="Human-readable transport name")
    category: TransportCategory = Field(..., description="Transport category")
    subcategory: Optional[str] = Field(None, description="Optional subcategory")
    description: str = Field(..., description="Detailed description")
    layer: int = Field(..., description="OSI layer (1-7)")
    medium: TransportMedium = Field(..., description="Physical medium type")
    typical_bandwidth: str = Field(..., description="e.g., '100 Mbps', '1 Gbps'")
    typical_range: str = Field(..., description="e.g., '10 meters', 'Unlimited'")
    latency_class: LatencyClass = Field(..., description="Expected latency class")
    detection_method: DetectionMethod = Field(..., description="How to detect this transport")
    adapter_family: str = Field(..., description="Hardware adapter family")
    is_common: bool = Field(..., description="Whether this is commonly deployed")
    requires_hardware: bool = Field(..., description="Whether specific hardware is required")
    duplex: str = Field(..., description="'full', 'half', 'simplex'")
    supports_multicast: bool = Field(..., description="Whether multicast is supported")
    supports_broadcast: bool = Field(..., description="Whether broadcast is supported")
    max_nodes: Optional[int] = Field(None, description="Maximum nodes if limited")
    security_level: SecurityLevel = Field(..., description="Native security level")

    class Config:
        use_enum_values = False


class DetectedTransport(BaseModel):
    """A transport detected on the local system."""
    transport_id: str = Field(..., description="Transport type ID")
    transport_name: str = Field(..., description="Transport type name")
    adapter_family: str = Field(..., description="Adapter family (e.g., 'ethernet', 'wifi')")
    interface_name: str = Field(..., description="System interface name (e.g., 'eth0', 'wlan0')")
    ip_address: Optional[str] = Field(None, description="Assigned IP address")
    subnet_mask: Optional[str] = Field(None, description="Subnet mask")
    gateway: Optional[str] = Field(None, description="Default gateway")
    mac_address: Optional[str] = Field(None, description="MAC address")
    speed_mbps: Optional[float] = Field(None, description="Link speed in Mbps")
    is_up: bool = Field(..., description="Whether interface is up")
    is_connected: bool = Field(..., description="Whether actively connected")
    signal_strength: Optional[int] = Field(None, ge=0, le=100, description="Signal strength 0-100")
    mtu: Optional[int] = Field(None, description="Maximum transmission unit")
    status: TransportStatus = Field(default=TransportStatus.AVAILABLE, description="Current status")
    detected_at: datetime = Field(default_factory=datetime.utcnow, description="Detection timestamp")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        use_enum_values = False


class BridgeConfig(BaseModel):
    """Configuration for a communication bridge."""
    bridge_id: str = Field(..., description="Unique bridge identifier")
    source_transport_id: str = Field(..., description="Transport to bridge on")
    name: str = Field(..., description="Human-readable bridge name")
    bind_address: str = Field(..., description="Address to bind to (e.g., '0.0.0.0')")
    bind_port: int = Field(..., ge=1, le=65535, description="Port to bind to")
    protocol: str = Field(default="tcp", description="'tcp', 'udp', or 'both'")
    encryption: bool = Field(default=False, description="Enable encryption")
    compression: bool = Field(default=False, description="Enable compression")
    max_connections: int = Field(default=100, ge=1, description="Max concurrent connections")
    heartbeat_interval_ms: int = Field(default=5000, ge=100, description="Peer heartbeat interval")
    reconnect_attempts: int = Field(default=3, ge=0, description="Reconnection attempts")
    buffer_size_bytes: int = Field(default=65536, ge=4096, description="I/O buffer size")

    class Config:
        use_enum_values = False


class BridgeStatus(BaseModel):
    """Current status of an active bridge."""
    bridge_id: str = Field(..., description="Bridge identifier")
    status: TransportStatus = Field(..., description="Current operational status")
    connected_peers: int = Field(default=0, ge=0, description="Number of connected peers")
    bytes_sent: int = Field(default=0, ge=0, description="Total bytes sent")
    bytes_received: int = Field(default=0, ge=0, description="Total bytes received")
    uptime_seconds: int = Field(default=0, ge=0, description="Uptime in seconds")
    last_activity: datetime = Field(default_factory=datetime.utcnow, description="Last activity timestamp")
    latency_ms: Optional[float] = Field(None, ge=0, description="Average latency")
    packet_loss_percent: Optional[float] = Field(None, ge=0, le=100, description="Packet loss percentage")
    error_count: int = Field(default=0, ge=0, description="Total errors encountered")

    class Config:
        use_enum_values = False


class SignalQuality(BaseModel):
    """Network signal quality metrics."""
    transport_id: str = Field(..., description="Transport identifier")
    signal_strength: Optional[int] = Field(None, ge=0, le=100, description="Signal strength 0-100")
    noise_level: Optional[float] = Field(None, ge=0, description="Noise level in dBm")
    snr_db: Optional[float] = Field(None, description="Signal-to-noise ratio in dB")
    bandwidth_available_mbps: Optional[float] = Field(None, ge=0, description="Available bandwidth")
    latency_ms: float = Field(..., ge=0, description="Latency in milliseconds")
    jitter_ms: Optional[float] = Field(None, ge=0, description="Jitter in milliseconds")
    packet_loss_percent: float = Field(default=0, ge=0, le=100, description="Packet loss percentage")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Measurement timestamp")

    class Config:
        use_enum_values = False
