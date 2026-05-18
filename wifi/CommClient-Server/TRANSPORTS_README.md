# CommClient Transport Abstraction Layer

Comprehensive network transport auto-detection, bridging, and capability assessment system for the CommClient LAN communication platform.

## Overview

The transport layer provides:

1. **TransportRegistry** — Central catalog of 30+ network transport types
2. **TransportDetector** — Auto-discovers available transports using 6+ detection methods
3. **BridgeManager** — Creates and manages communication bridges on detected transports
4. **SignalAnalyzer** — Measures network quality (latency, bandwidth, jitter, packet loss)
5. **TransportCapabilities** — Evaluates service compatibility based on transport characteristics

## Architecture

```
app/transports/
├── __init__.py              # Package initialization and exports
├── types.py                 # Pydantic models and enums (30+ types)
├── registry.py              # TransportRegistry singleton
├── detector.py              # TransportDetector singleton
├── bridge.py                # BridgeManager singleton
├── signal.py                # SignalAnalyzer singleton
└── capabilities.py          # TransportCapabilities utility class

config/
├── transport_catalog.json   # Transport definitions
└── detection_rules.json     # Detection rules per adapter family
```

## Core Components

### 1. TransportRegistry

Centralized catalog of network transport definitions.

**Key Methods:**
- `get_all_transports()` — Get all registered transports
- `get_by_category(category)` — Filter by category (ethernet, wifi, bluetooth, etc.)
- `get_by_adapter_family(family)` — Filter by adapter type
- `get_by_medium(medium)` — Filter by physical medium (wired, wireless, optical, etc.)
- `get_common_transports()` — Get commonly deployed transports only
- `search(query)` — Full-text search by name/description
- `get_categories()` — Get list of categories with counts
- `get_statistics()` — Get registry statistics

**Usage:**
```python
from app.transports import TransportRegistry

registry = TransportRegistry()

# Get all transports
all_transports = registry.get_all_transports()

# Get specific transports
wifi_transports = registry.get_by_category("wifi_80211")
ethernet = registry.get_transport("ethernet")

# Search
results = registry.search("wireless")

# Get statistics
stats = registry.get_statistics()
print(f"Total transports: {stats['total_transports']}")
print(f"Categories: {stats['by_category']}")
```

### 2. TransportDetector

Discovers available network transports on the system using multiple methods:
- Network interface scanning (psutil)
- Wi-Fi discovery (netsh, iw)
- USB device detection
- Serial port scanning
- Service port probing
- Bluetooth/BLE detection

Performs detection in parallel and maintains cached results.

**Key Methods:**
- `detect_all()` — Run all detection methods in parallel
- `detect_by_family(family)` — Detect specific adapter family
- `get_cached_results()` — Get last detection results (synchronous)
- `get_best_transport()` — Get highest quality available transport
- `start_auto_refresh(interval_seconds)` — Start background refresh loop
- `stop_auto_refresh()` — Stop auto-refresh

**Usage:**
```python
from app.transports import TransportDetector, TransportRegistry

registry = TransportRegistry()
detector = TransportDetector(registry)

# Run detection
detected = await detector.detect_all()
print(f"Found {len(detected)} transports")

for transport in detected:
    print(f"  {transport.transport_name} ({transport.interface_name})")
    print(f"    IP: {transport.ip_address}")
    print(f"    Speed: {transport.speed_mbps} Mbps")
    print(f"    Connected: {transport.is_connected}")

# Get best transport
best = detector.get_best_transport()
if best:
    print(f"Best transport: {best.transport_name}")

# Auto-refresh every 30 seconds
await detector.start_auto_refresh(interval_seconds=30)

# Later: stop refresh
await detector.stop_auto_refresh()
```

### 3. BridgeManager

Creates and manages communication bridges on detected transports.

**Key Methods:**
- `create_bridge(config)` — Create a new bridge
- `destroy_bridge(bridge_id)` — Tear down a bridge
- `get_bridge_status(bridge_id)` — Get bridge status
- `get_all_bridges()` — List all active bridges
- `auto_bridge()` — Create bridge on best transport
- `relay_data(bridge_id, data, target_peer)` — Relay data to peer
- `broadcast(bridge_id, data)` — Broadcast to all peers

**Usage:**
```python
from app.transports import BridgeManager, BridgeConfig, TransportDetector, TransportRegistry

registry = TransportRegistry()
detector = TransportDetector(registry)

# Run detection first
await detector.detect_all()

manager = BridgeManager(detector)

# Create bridge manually
config = BridgeConfig(
    bridge_id="bridge-1",
    source_transport_id="wifi_80211",
    name="Primary Bridge",
    bind_address="0.0.0.0",
    bind_port=5555,
    protocol="tcp",
    encryption=False,
    compression=False,
)

status = await manager.create_bridge(config)
print(f"Bridge status: {status.status}")

# Or auto-create on best transport
auto_status = await manager.auto_bridge()

# Relay data
await manager.relay_data("bridge-1", b"hello", "peer-123")

# Broadcast
peer_count = await manager.broadcast("bridge-1", b"broadcast message")

# Check status
status = await manager.get_bridge_status("bridge-1")
print(f"Connected peers: {status.connected_peers}")
print(f"Bytes sent: {status.bytes_sent}")

# Clean up
await manager.destroy_bridge("bridge-1")
```

### 4. SignalAnalyzer

Measures network quality metrics for signal assessment.

**Key Methods:**
- `measure_latency(target_ip, count)` — Measure latency in ms
- `measure_bandwidth(target_ip)` — Measure bandwidth in Mbps
- `measure_jitter(target_ip, count)` — Measure jitter in ms
- `measure_packet_loss(target_ip, count)` — Measure packet loss %
- `full_analysis(transport)` — Complete quality analysis
- `continuous_monitor(transport_id, callback, interval)` — Continuous monitoring
- `get_quality_score(quality)` — Get score 0-100
- `get_quality_label(score)` — Get label (excellent/good/fair/poor/unusable)

**Usage:**
```python
from app.transports import SignalAnalyzer

analyzer = SignalAnalyzer()

# Measure individual metrics
latency = await analyzer.measure_latency("192.168.1.1", count=5)
print(f"Latency: {latency:.2f} ms")

bandwidth = await analyzer.measure_bandwidth("192.168.1.1")
print(f"Bandwidth: {bandwidth:.2f} Mbps")

jitter = await analyzer.measure_jitter("192.168.1.1", count=10)
print(f"Jitter: {jitter:.2f} ms")

packet_loss = await analyzer.measure_packet_loss("192.168.1.1", count=20)
print(f"Packet loss: {packet_loss:.1f}%")

# Full analysis
quality = await analyzer.full_analysis(detected_transport)
print(f"Latency: {quality.latency_ms} ms")
print(f"Bandwidth: {quality.bandwidth_available_mbps} Mbps")

# Quality scoring
score = analyzer.get_quality_score(quality)
label = analyzer.get_quality_label(score)
print(f"Quality score: {score}/100 ({label})")
```

### 5. TransportCapabilities

Evaluates service compatibility with transport characteristics.

**Key Methods:**
- `can_support_voice(transport, bitrate_kbps)` — Voice call support
- `can_support_video(transport, bitrate_kbps)` — Video call support
- `can_support_screen_share(transport, bitrate_kbps)` — Screen sharing
- `can_support_file_transfer(transport)` — File transfer
- `can_support_group_call(transport, participant_count)` — Group call with N participants
- `get_max_participants(transport)` — Max participants for group calls
- `get_recommended_codec(transport)` — Recommend audio/video codec
- `get_recommended_quality(transport)` — Recommend resolution, framerate, bitrate

**Usage:**
```python
from app.transports import TransportCapabilities, TransportDetector, TransportRegistry

registry = TransportRegistry()
detector = TransportDetector(registry)
await detector.detect_all()

transport = detector.get_cached_results()[0]

# Check capabilities
caps = TransportCapabilities()

if caps.can_support_voice(transport):
    print("Voice calling supported")

if caps.can_support_video(transport):
    print("Video calling supported")

if caps.can_support_group_call(transport, participant_count=5):
    print("Group call with 5 participants supported")

max_participants = caps.get_max_participants(transport)
print(f"Max participants: {max_participants}")

# Get recommendations
codec_rec = caps.get_recommended_codec(transport)
print(f"Audio codec: {codec_rec['audio']['codec']}")
print(f"Audio bitrate: {codec_rec['audio']['bitrate_kbps']} kbps")

quality_rec = caps.get_recommended_quality(transport)
print(f"Video resolution: {quality_rec['resolution']}")
print(f"Framerate: {quality_rec['framerate']} fps")
print(f"Bitrate: {quality_rec['bitrate_kbps']} kbps")
```

## Data Types

All data is represented using Pydantic models for validation and serialization.

### TransportDefinition
Catalog definition of a transport type.

```python
{
    "id": "wifi_80211",
    "name": "Wi-Fi (802.11a/b/g/n)",
    "category": "wifi_80211",
    "layer": 2,
    "medium": "wireless",
    "typical_bandwidth": "6-600 Mbps",
    "latency_class": "low",
    "adapter_family": "wifi",
    "is_common": True,
    "requires_hardware": True,
    "duplex": "half",
    "supports_multicast": True,
    "supports_broadcast": True,
    "security_level": "medium",
}
```

### DetectedTransport
Runtime instance of a detected transport.

```python
{
    "transport_id": "wifi_80211",
    "transport_name": "Wi-Fi (802.11)",
    "adapter_family": "wifi",
    "interface_name": "wlan0",
    "ip_address": "192.168.1.100",
    "subnet_mask": "255.255.255.0",
    "gateway": "192.168.1.1",
    "mac_address": "aa:bb:cc:dd:ee:ff",
    "speed_mbps": 72.2,
    "is_up": True,
    "is_connected": True,
    "signal_strength": 85,
    "mtu": 1500,
    "status": "active",
    "detected_at": "2026-04-09T12:34:56.000000",
    "metadata": {...}
}
```

### BridgeConfig & BridgeStatus
Bridge configuration and runtime status.

```python
# Config
{
    "bridge_id": "bridge-1",
    "source_transport_id": "wifi_80211",
    "name": "Primary Bridge",
    "bind_address": "0.0.0.0",
    "bind_port": 5555,
    "protocol": "tcp",
    "encryption": False,
    "compression": False,
    "max_connections": 100,
}

# Status
{
    "bridge_id": "bridge-1",
    "status": "active",
    "connected_peers": 3,
    "bytes_sent": 102400,
    "bytes_received": 204800,
    "uptime_seconds": 1234,
    "latency_ms": 5.2,
    "packet_loss_percent": 0.1,
    "error_count": 0,
}
```

### SignalQuality
Network quality metrics.

```python
{
    "transport_id": "wifi_80211",
    "signal_strength": 85,
    "snr_db": 25.0,
    "bandwidth_available_mbps": 72.2,
    "latency_ms": 5.2,
    "jitter_ms": 1.1,
    "packet_loss_percent": 0.1,
    "timestamp": "2026-04-09T12:34:56.000000",
}
```

## Enums

**TransportMedium:**
- wired, wireless, optical, virtual, hybrid

**LatencyClass:**
- ultra_low (<5ms), low (5-50ms), medium (50-150ms), high (150-500ms), very_high (>500ms)

**SecurityLevel:**
- none, basic, medium, high, military

**DetectionMethod:**
- interface_scan, driver_check, port_scan, service_discovery, hardware_probe, manual, api_query

**TransportStatus:**
- available, active, degraded, unavailable, error

**TransportCategory:** (30 types)
ethernet, wifi_80211, wifi_6ghz, lte_4g, 5g, wimax, zigbee, zwave, lora, sigfox, nb_iot, cellular_2g, cellular_3g, bluetooth_classic, ble, bluetooth_mesh, thread, matter, modbus, bacnet, dali, knx, profibus, profinet, powerline, serial_rs232, usb, optical_fiber, satellite, custom

## Configuration Files

### transport_catalog.json
Defines all available transport types with their characteristics.

### detection_rules.json
Detection rules per adapter family:
- Patterns to match in interface names
- Detection methods to try
- Keywords to search for

## Integration Example

Complete example of using the transport layer:

```python
from app.transports import (
    TransportRegistry,
    TransportDetector,
    BridgeManager,
    SignalAnalyzer,
    TransportCapabilities,
    BridgeConfig,
)

async def setup_communication():
    """Set up optimal communication transport."""
    
    # Initialize components
    registry = TransportRegistry()
    detector = TransportDetector(registry)
    analyzer = SignalAnalyzer()
    manager = BridgeManager(detector)
    caps = TransportCapabilities()
    
    # Detect available transports
    detected = await detector.detect_all()
    logger.info(f"Found {len(detected)} transports")
    
    # Analyze best transport
    best = detector.get_best_transport()
    if best:
        logger.info(f"Best transport: {best.transport_name}")
        
        # Measure signal quality
        quality = await analyzer.full_analysis(best)
        score = analyzer.get_quality_score(quality)
        label = analyzer.get_quality_label(score)
        logger.info(f"Quality: {score}/100 ({label})")
        
        # Check capabilities
        if caps.can_support_video(best):
            rec = caps.get_recommended_quality(best)
            logger.info(f"Video: {rec['resolution']} @ {rec['framerate']} fps")
        
        # Create bridge
        config = BridgeConfig(
            bridge_id="main",
            source_transport_id=best.transport_id,
            name="Primary Communication Bridge",
            bind_address="0.0.0.0",
            bind_port=5555,
            protocol="tcp",
        )
        
        status = await manager.create_bridge(config)
        logger.info(f"Bridge created: {status.status}")
        
        # Start auto-refresh
        await detector.start_auto_refresh(interval_seconds=30)
        
        return {
            "detector": detector,
            "manager": manager,
            "analyzer": analyzer,
            "best_transport": best,
            "quality": quality,
        }

# Usage
result = await setup_communication()
```

## Performance Notes

- **Detection**: Runs in parallel, typically completes in 1-5 seconds
- **Signal Analysis**: Takes 20-30 seconds for comprehensive metrics
- **Bridge Operations**: Sub-millisecond for data relay
- **Memory**: Minimal footprint, singletons share state

## Error Handling

All async methods return gracefully on errors:
- Detection failures log warnings but continue
- Measurement failures return -1.0 or None
- Bridge operations return status/error in response

## Thread Safety

- `TransportDetector`: Thread-safe with asyncio.Lock
- `BridgeManager`: Thread-safe with asyncio.Lock
- All other singletons use standard Python locks as needed

## Testing

Example test usage:

```python
import pytest
from app.transports import TransportRegistry

@pytest.mark.asyncio
async def test_transport_registry():
    registry = TransportRegistry()
    
    all_transports = registry.get_all_transports()
    assert len(all_transports) > 0
    
    wifi = registry.get_by_category("wifi_80211")
    assert len(wifi) > 0
    
    stats = registry.get_statistics()
    assert stats["total_transports"] > 0
```

## Future Enhancements

- [ ] Machine learning for quality prediction
- [ ] Network topology mapping
- [ ] Automatic failover with load balancing
- [ ] QoS monitoring and enforcement
- [ ] Transport cost optimization
- [ ] Mobile handoff support
- [ ] Network slicing
