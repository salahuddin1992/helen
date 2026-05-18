# Network Topology — Helen Mesh

> تصميم Network Topology عميق مقسّم إلى ملفات مستقلة قابلة للإضافة.
> التنفيذ: `app/topology/` package + الـ services الموجودة في `app/services/`.

---

## 1. تحليل نوع الـ Topology

### مقارنة الأنواع

| النوع | المزايا | العيوب | يصلح لـ |
|---|---|---|---|
| **Bus** | بساطة | نقطة فشل واحدة | شبكات صغيرة جدًا |
| **Star** | إدارة مركزية | hub failure = collapse | datacenters قديمة |
| **Ring** | predictable latency | break = full outage | token ring legacy |
| **Tree** | hierarchy clean | root failure | corporate WAN |
| **Full Mesh** | maximum redundancy | n² links | small clusters ≤ 50 |
| **Partial Mesh** | scalable | not all-pairs reachable | medium 50-1000 |
| **Hybrid** | best of all | complexity | production systems |
| **Overlay** | NAT-friendly | adds latency | P2P / WAN |
| **Federated** | autonomy + interop | secret sharing | multi-cluster |

### القرار النهائي: **Hybrid Overlay Mesh Topology**

```
محليًا (نفس الـ subnet):     Full Mesh عبر UDP broadcast
بين السيرفرات (cluster):    Partial Mesh مع gossip K=10
بين الشبكات (clusters):     Federation Overlay مع HMAC
عند الفشل:                 Multi-hop Relay (4 hops × 8 proxies)
خلف NAT:                   Rendezvous + reverse tunnel
```

**لماذا هذا الاختيار؟**
- LAN deployments تستفيد من Full Mesh (لا overhead، broadcast مجاني)
- Cluster scale يستفيد من Partial Mesh (gossip O(log N) convergence)
- Multi-cluster/cross-router يستفيد من Federation (cluster_id isolation)
- Failure resilience من Multi-hop (4096 paths/pair)
- WAN/NAT يستفيد من Overlay (tunneling layer)

---

## 2. الطبقات العشر (10-Layer Topology Stack)

```
┌─────────────────────────────────────────────────────┐
│ Layer 10: Recovery       (self-healing, isolation)  │
├─────────────────────────────────────────────────────┤
│ Layer 9:  Monitoring     (health, metrics, snap)    │
├─────────────────────────────────────────────────────┤
│ Layer 8:  Security       (HMAC, keys, trust)        │
├─────────────────────────────────────────────────────┤
│ Layer 7:  Federation     (cluster ↔ cluster)        │
├─────────────────────────────────────────────────────┤
│ Layer 6:  NAT Traversal  (hole-punch, tunnel)       │
├─────────────────────────────────────────────────────┤
│ Layer 5:  Routing        (multi-path, failover)     │
├─────────────────────────────────────────────────────┤
│ Layer 4:  Overlay        (peer graph, route table)  │
├─────────────────────────────────────────────────────┤
│ Layer 3:  Discovery      (UDP/mDNS/Gossip/DHT)      │
├─────────────────────────────────────────────────────┤
│ Layer 2:  Local Network  (LAN, subnets, broadcast)  │
├─────────────────────────────────────────────────────┤
│ Layer 1:  Physical       (Routers, NICs, WiFi/USB)  │
└─────────────────────────────────────────────────────┘
```

### تفاصيل كل طبقة

| الطبقة | الملفات/الـmodules المسؤولة |
|---|---|
| **Physical** | بنية تحتية — لا code، يكشفها `psutil.net_if_addrs` |
| **Local Network** | `topology/subnet_model.py`, `topology/router_model.py` |
| **Discovery** | `services/discovery_service.py`, `services/mdns_discovery.py`, `services/peer_registry.py`, `services/bloom_discovery.py` |
| **Overlay** | `topology/topology_graph.py`, `topology/node_model.py`, `topology/link_model.py` |
| **Routing** | `services/multipath_router.py`, `services/cluster_mesh.py`, `services/path_health.py`, `services/load_balancer.py` |
| **NAT Traversal** | `services/connectivity/*` (hole_punch, reverse_tunnel, relay) |
| **Federation** | `api/routes/federation.py`, `services/federation_service.py`, `core/federation_auth.py` |
| **Security** | `core/federation_auth.py`, `services/sync_policy.py`, `services/trust_score.py`, `services/audit_replication.py` |
| **Monitoring** | `services/metrics_export.py`, `services/path_health.py`, `services/phi_accrual.py` |
| **Recovery** | `services/state_reconciliation.py`, `services/anti_entropy.py`, `services/partition_detector.py` |

---

## 3. هيكل الملفات الكامل (Helen-Server actual layout)

```
CommClient-Server/
│
├── app/
│   ├── main.py                              ← lifespan startup hooks
│   │
│   ├── core/
│   │   ├── config.py                        ← Settings (BaseSettings)
│   │   ├── federation_auth.py               ← HMAC sign/verify + cluster_id key
│   │   └── logging.py                       ← structlog setup
│   │
│   ├── topology/                            ← NEW (هذه الجولة)
│   │   ├── __init__.py
│   │   ├── node_model.py                    ← Node + NodeType enum
│   │   ├── link_model.py                    ← Link مع metrics
│   │   ├── subnet_model.py                  ← Subnet detection from IP
│   │   ├── router_model.py                  ← Router node
│   │   ├── bridge_model.py                  ← Bridge node (multi-NIC)
│   │   ├── topology_graph.py                ← Graph + traversal
│   │   ├── topology_store.py                ← JSON persistence
│   │   ├── topology_manager.py              ← Coordinator
│   │   └── topology_visualizer.py           ← ASCII / mermaid renderers
│   │
│   ├── services/                            ← الموجود (24 ملف mesh)
│   │   ├── path_health.py                   ← passive latency
│   │   ├── latency_prober.py                ← active probing
│   │   ├── load_balancer.py                 ← weighted ranking
│   │   ├── adaptive_timeout.py              ← RFC 6298 RTO
│   │   ├── consistent_hash.py               ← sharding ring
│   │   ├── multipath_router.py              ← top-K routing
│   │   ├── trust_score.py                   ← persistent reputation
│   │   ├── state_reconciliation.py          ← LWW convergence
│   │   ├── anti_entropy.py                  ← Merkle diff
│   │   ├── replication_manager.py           ← K-replica
│   │   ├── quorum_decision.py               ← K-acks write
│   │   ├── distributed_lock.py              ← cluster-wide lease
│   │   ├── partition_detector.py            ← split-brain
│   │   ├── cluster_time.py                  ← consensus offset
│   │   ├── backpressure.py                  ← overload gate
│   │   ├── phi_accrual.py                   ← failure detection
│   │   ├── sync_policy.py                   ← block / pause
│   │   ├── audit_replication.py             ← hash-chained log
│   │   ├── log_compaction.py                ← archive old entries
│   │   ├── metrics_export.py                ← Prometheus exporter
│   │   ├── mdns_discovery.py                ← Bonjour
│   │   ├── bloom_discovery.py               ← compact membership
│   │   ├── vector_clock.py                  ← causal ordering
│   │   └── crdt_state.py                    ← conflict-free types
│   │
│   ├── api/
│   │   └── routes/
│   │       ├── cluster.py                   ← public cluster endpoints
│   │       ├── federation.py                ← signed federation ops
│   │       └── admin_peers.py               ← admin-gated tools
│   │
│   └── db/
│       ├── sqlite_tuning.py
│       └── …
│
├── data/
│   ├── topology.json                        ← topology_store output
│   ├── peers_cache.json
│   ├── trust_db.sqlite
│   ├── audit_chain.jsonl
│   ├── audit_archive/
│   ├── replicated_state.sqlite
│   ├── sync_policy.json
│   └── cluster_high_water.json
│
├── docs/
│   ├── MESH_ARCHITECTURE.md                 ← مرجع المعمارية
│   ├── MULTIPATH_ROUTING.md                 ← تصميم الراوتر
│   └── NETWORK_TOPOLOGY.md                  ← هذه الوثيقة
│
└── tests/
    └── test_multipath_router.py
    └── test_topology.py                     ← NEW (هذه الجولة)
```

---

## 4. النموذج البرمجي (Topology Object Model)

### Node

```python
class NodeType(str, Enum):
    CLIENT          = "client"           # PC / phone
    PEER            = "peer"             # Helen-Server (full role)
    ROUTER          = "router"           # IP router box
    BRIDGE          = "bridge"           # multi-NIC peer
    DISCOVERY       = "discovery"        # broadcast advertiser
    RELAY           = "relay"            # passive byte forwarder
    PROXY           = "proxy"            # active HTTP forwarder
    FEDERATION      = "federation"       # cross-cluster gateway
    DHT             = "dht"              # Kademlia node
    RENDEZVOUS      = "rendezvous"       # NAT traversal hub

@dataclass
class Node:
    node_id:    str
    node_type:  NodeType
    host:       str
    port:       int
    subnet:     Optional[str]
    nics:       list[str]
    cluster_id: str
    capabilities: dict       # cores, ram, nic_gbps
    roles:        set[str]   # signaling, sfu, relay, ...
```

### Link

```python
@dataclass
class Link:
    src_id:       str
    dst_id:       str
    link_type:    LinkType   # LAN / BRIDGE / RELAY / TUNNEL / FEDERATION
    latency_ms:   float
    bandwidth_mbps: float
    packet_loss:  float
    last_seen:    float
    score:        float
```

### Subnet

```python
@dataclass
class Subnet:
    cidr:        str         # "192.168.1.0/24"
    gateway:     Optional[str]
    nodes:       set[str]    # node_ids resident here
    is_local:    bool
```

### Topology Graph

```python
class TopologyGraph:
    nodes: dict[str, Node]
    links: dict[(str, str), Link]
    subnets: dict[str, Subnet]
    
    def neighbors(self, node_id: str) -> list[Node]
    def shortest_path(self, src, dst) -> list[Node]
    def k_shortest_paths(self, src, dst, k=4) -> list[list[Node]]
    def cross_subnet_bridges(self) -> list[Node]
    def partition_components(self) -> list[set[str]]
```

---

## 5. مثال طبوغرافي عملي

### السيناريو

- 10 PCs (clients)
- 10 Helen-Servers (peers)
- 10 Routers (3 منهم gateways، 7 internal switches)
- 3 Subnets: 192.168.1.0/24 / 10.0.0.0/24 / 172.16.0.0/24
- 3 NAT types: symmetric / full-cone / restricted
- 2 bridges: S4 (USB-tether)، S7 (fiber)

### Topology Graph

```
┌─────────────────── INTERNET ───────────────────┐
│                                                 │
│   ┌────────┐     ┌────────┐     ┌────────┐    │
│   │NAT-A   │     │NAT-B   │     │NAT-C   │    │
│   │symm    │     │full    │     │restr   │    │
│   └────┬───┘     └────┬───┘     └────┬───┘    │
└────────┼──────────────┼──────────────┼────────┘
         │              │              │
   192.168.1.0/24  10.0.0.0/24   172.16.0.0/24
         │              │              │
    ┌────┴────┐    ┌────┴────┐    ┌────┴────┐
    │R1-R4    │    │R5-R7    │    │R8-R10   │
    │S1,S2,S3,│    │S5,S6,S7 │    │S8,S9,S10│
    │S4(brg)→ │ ←→ │ ←(brg)→ │ ←→ │         │
    │C1,C2,C3,│    │C5,C6,C7 │    │C8,C9,C10│
    │C4       │    │         │    │         │
    └─────────┘    └─────────┘    └─────────┘
```

---

## 6. الـ Topology API

```
GET  /api/topology/snapshot       → full graph dump (JSON)
GET  /api/topology/visualize      → ASCII renderer
GET  /api/topology/neighbors/{id} → adjacency list
GET  /api/topology/path/{a}/{b}   → k-shortest paths
GET  /api/topology/partitions     → connected components
GET  /api/topology/subnets        → subnets + node memberships
GET  /api/topology/bridges        → list multi-NIC bridge nodes
```

---

## 7. تكامل الطبقات

```
discovery_service.broadcast() ──┐
peer_registry.ingest()         ─┼──▶ topology_manager.absorb_peer()
mdns_discovery.peer_found()    ─┘
                                       │
                                       ▼
                            topology_graph.add_node()
                            topology_graph.add_link()
                                       │
                                       ▼
                            topology_store.persist()
                                       │
                                       ▼
                       multipath_router.discover_routes() ◀──── reads
                       partition_detector._check_once()  ◀──── reads
                       metrics_export.render_prometheus() ◀── reads
```

---

## 8. الخصائص الفنية المضمونة

| الخاصية | كيف نضمنها |
|---|---|
| **No SPOF** | كل دور مكرر (DHT K=20، gossip K=10، replicas K=3) |
| **Self-healing** | partition_detector + state_reconciliation + anti_entropy |
| **Bounded latency** | adaptive_timeout (RFC 6298) per peer |
| **Convergence** | gossip + Merkle anti-entropy + LWW |
| **Tamper-evident audit** | hash-chained log + replication 3-fanout |
| **Causal ordering** | vector_clock + crdt_state |
| **Partition tolerance** | quorum-aware writes + minority read-only mode |
| **Observable** | Prometheus + structured JSON logs + audit trail |

---

*التنفيذ الفعلي في `app/topology/` (هذه الجولة) + `app/services/` (الجولات السابقة).*
