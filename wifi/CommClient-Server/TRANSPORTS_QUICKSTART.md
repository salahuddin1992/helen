# Transport Layer Quick Start

## Installation

The transport layer is part of CommClient and requires:
- Python 3.9+
- psutil (for network interface scanning)
- pydantic (for type validation)
- asyncio (built-in)

Ensure requirements.txt includes psutil:
```bash
pip install psutil
```

## 5-Minute Setup

### Step 1: Import Components

```python
from app.transports import (
    TransportRegistry,
    TransportDetector,
    BridgeManager,
    SignalAnalyzer,
    TransportCapabilities,
)
from app.core.logging import get_logger

logger = get_logger(__name__)
```

### Step 2: Initialize Singletons

```python
# Create singletons (they're lazy-initialized on first use)
registry = TransportRegistry()
detector = TransportDetector(registry)
analyzer = SignalAnalyzer()
manager = BridgeManager(detector)
caps = TransportCapabilities()
```

### Step 3: Detect Transports

```python
# Run detection (fully async)
detected_transports = await detector.detect_all()

logger.info(f"Detected {len(detected_transports)} transports")
for transport in detected_transports:
    logger.info(f"  {transport.interface_name}: {transport.transport_name}")
    logger.info(f"    IP: {transport.ip_address}")
    logger.info(f"    Speed: {transport.speed_mbps} Mbps")
    logger.info(f"    Connected: {transport.is_connected}")
```

### Step 4: Create Communication Bridge

```python
from app.transports import BridgeConfig

# Get best transport
best = detector.get_best_transport()
if not best:
    logger.error("No transport available")
    return

logger.info(f"Using transport: {best.transport_name}")

# Create bridge
config = BridgeConfig(
    bridge_id="main",
    source_transport_id=best.transport_id,
    name="Primary Bridge",
    bind_address="0.0.0.0",
    bind_port=5555,
    protocol="tcp",
)

status = await manager.create_bridge(config)
logger.info(f"Bridge status: {status.status}")
```

### Step 5: Monitor Quality (Optional)

```python
# Analyze signal quality
quality = await analyzer.full_analysis(best)

score = analyzer.get_quality_score(quality)
label = analyzer.get_quality_label(score)

logger.info(f"Quality: {score}/100 ({label})")
logger.info(f"  Latency: {quality.latency_ms:.1f} ms")
logger.info(f"  Jitter: {quality.jitter_ms:.1f} ms")
logger.info(f"  Packet loss: {quality.packet_loss_percent:.1f}%")
```

### Step 6: Check Capabilities

```python
# Check what services this transport can support
if caps.can_support_video(best):
    rec = caps.get_recommended_quality(best)
    logger.info(f"Video: {rec['resolution']} @ {rec['framerate']} fps")

if caps.can_support_voice(best):
    logger.info("Voice calling supported")

max_participants = caps.get_max_participants(best)
logger.info(f"Max group call participants: {max_participants}")
```

## Common Tasks

### Get All Available Transports

```python
all_transports = registry.get_all_transports()
for t in all_transports:
    print(f"{t.id}: {t.name} ({t.adapter_family})")
```

### Filter by Type

```python
# Wireless transports
wireless = registry.get_by_medium("wireless")

# Common transports only
common = registry.get_common_transports()

# Search
results = registry.search("ethernet")
```

### Real-Time Monitoring

```python
# Start auto-refresh every 30 seconds
await detector.start_auto_refresh(interval_seconds=30)

# Later queries will get fresh data
cached = detector.get_cached_results()

# Stop when done
await detector.stop_auto_refresh()
```

### Bridge Management

```python
# List all bridges
all_bridges = await manager.get_all_bridges()
for bridge in all_bridges:
    print(f"{bridge.bridge_id}: {bridge.connected_peers} peers")

# Broadcast data
peer_count = await manager.broadcast("main", b"hello everyone")
print(f"Message sent to {peer_count} peers")

# Clean up
await manager.destroy_bridge("main")
```

### Measure Network Metrics

```python
# Measure to specific IP
latency = await analyzer.measure_latency("192.168.1.1", count=5)
bandwidth = await analyzer.measure_bandwidth("192.168.1.1")
packet_loss = await analyzer.measure_packet_loss("192.168.1.1")

print(f"Latency: {latency:.2f} ms")
print(f"Bandwidth: {bandwidth:.2f} Mbps")
print(f"Packet loss: {packet_loss:.1f}%")
```

## Integration with FastAPI

### Add Routes

```python
from fastapi import APIRouter, HTTPException
from app.transports import (
    TransportDetector,
    TransportRegistry,
    SignalAnalyzer,
)

router = APIRouter(prefix="/api/transports", tags=["transports"])

# Initialize (at app startup)
registry = TransportRegistry()
detector = TransportDetector(registry)
analyzer = SignalAnalyzer()

@router.get("/detect")
async def detect_transports():
    """Detect available transports."""
    transports = await detector.detect_all()
    return {
        "count": len(transports),
        "transports": [t.dict() for t in transports],
    }

@router.get("/best")
async def get_best_transport():
    """Get best available transport."""
    best = detector.get_best_transport()
    if not best:
        raise HTTPException(status_code=404, detail="No transport available")
    return best.dict()

@router.get("/catalog")
async def get_catalog():
    """Get transport catalog."""
    transports = registry.get_all_transports()
    return {
        "count": len(transports),
        "transports": [t.dict() for t in transports],
    }

@router.get("/quality/{transport_id}")
async def analyze_quality(transport_id: str):
    """Analyze quality of detected transport."""
    detected = detector.get_cached_results()
    transport = next(
        (t for t in detected if t.transport_id == transport_id),
        None,
    )
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")
    
    quality = await analyzer.full_analysis(transport)
    score = analyzer.get_quality_score(quality)
    
    return {
        "quality": quality.dict(),
        "score": score,
        "label": analyzer.get_quality_label(score),
    }
```

### Startup/Shutdown

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await detector.start_auto_refresh(interval_seconds=30)
    logger.info("Transport auto-refresh started")
    
    yield
    
    # Shutdown
    await detector.stop_auto_refresh()
    logger.info("Transport auto-refresh stopped")

app = FastAPI(lifespan=lifespan)
```

## Docker/Container Notes

When running in containers, some detection methods may be limited:

```python
# Gracefully handle container environments
detected = await detector.detect_all()

# Will detect container network interfaces
# Serial/USB detection may be disabled
# Bluetooth detection will fail gracefully

# Proceed with available transports
if detected:
    best = detector.get_best_transport()
else:
    logger.warning("No transports detected (check container network)")
```

## Production Checklist

- [ ] Configure transport catalog in config/transport_catalog.json
- [ ] Set detection rules in config/detection_rules.json
- [ ] Add psutil to requirements.txt
- [ ] Initialize detector and manager in app startup
- [ ] Add API endpoints for transport info
- [ ] Set up monitoring for signal quality
- [ ] Configure auto-refresh interval (default 30s)
- [ ] Add error handling for detection failures
- [ ] Test failover scenarios
- [ ] Monitor bridge statistics in production

## Troubleshooting

### No transports detected

```python
# Check registry loaded correctly
registry = TransportRegistry()
all_transports = registry.get_all_transports()
print(f"Catalog has {len(all_transports)} transports defined")

# Check system interfaces directly
import psutil
for interface, addrs in psutil.net_if_addrs().items():
    print(f"{interface}: {addrs}")
```

### Bridge creation fails

```python
# Verify transport exists
best = detector.get_best_transport()
if not best:
    print("No suitable transport found")
    # Run fresh detection
    await detector.detect_all()
    best = detector.get_best_transport()

# Check port availability
import socket
sock = socket.socket()
try:
    sock.bind(("0.0.0.0", 5555))
    print("Port 5555 available")
except OSError as e:
    print(f"Port 5555 in use: {e}")
finally:
    sock.close()
```

### Quality measurements timeout

```python
# Increase timeout or reduce count
latency = await analyzer.measure_latency(
    target_ip,
    count=3,  # Fewer pings
    timeout_seconds=20  # Longer timeout
)

# Check network connectivity
import subprocess
proc = await asyncio.create_subprocess_exec(
    "ping", "-c", "1", target_ip,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
print(stdout.decode())
```

## API Examples

### cURL Examples

```bash
# Detect transports
curl http://localhost:8000/api/transports/detect

# Get best transport
curl http://localhost:8000/api/transports/best

# Get catalog
curl http://localhost:8000/api/transports/catalog

# Analyze quality
curl http://localhost:8000/api/transports/quality/wifi_80211
```

### Python Client Example

```python
import httpx
import asyncio

async def fetch_transports():
    async with httpx.AsyncClient() as client:
        # Detect
        r = await client.get("http://localhost:8000/api/transports/detect")
        detected = r.json()
        print(f"Found {detected['count']} transports")
        
        # Get best
        r = await client.get("http://localhost:8000/api/transports/best")
        best = r.json()
        print(f"Best: {best['transport_name']}")
        
        # Analyze quality
        r = await client.get(
            f"http://localhost:8000/api/transports/quality/{best['transport_id']}"
        )
        quality = r.json()
        print(f"Quality score: {quality['score']}/100")

asyncio.run(fetch_transports())
```

## Next Steps

1. Read [TRANSPORTS_README.md](TRANSPORTS_README.md) for comprehensive documentation
2. Check integration examples in the main application
3. Review config files in `config/` directory
4. Run tests with pytest
5. Monitor transport behavior in production
