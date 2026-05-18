# Multi-Path Routing — Helen Mesh Network

> تصميم متقدّم لـ multi-path routing في نظام موزّع Mesh / P2P.
> التنفيذ الفعلي: `app/services/multipath_router.py` + الوحدات المرتبطة.

---

## 1. هدف النظام

| الهدف | كيف نحقّقه |
|---|---|
| **تعدّد المسارات** | 10 أنواع مسارات تُولَّد لكل target ⇒ ≥ 4,096 مسار محتمل |
| **تجاوز الفشل (failover)** | trial sequence top-K + cooldown 30s لكل مسار فشل |
| **تقليل التأخير** | EWMA latency tracking + RFC 6298 RTO + best-first ordering |
| **توزيع الحمل** | weighted ranking (load_balancer) + headroom signal |
| **ضمان الوصول** | dead_letter_service + persistent retry queue exponential |

```python
# app/services/multipath_router.py:send()
status, body, headers = await multipath_router.send(
    target_node_id="abc...", method="POST", path="/api/x", body={}
)
```

---

## 2. أنواع المسارات (10 RouteType)

| # | النوع | متى يُولَّد | المميِّز |
|---|---|---|---|
| 1 | `DIRECT` | دائمًا | `http://target.host:port` |
| 2 | `LAN_ALIAS` | peer multi-NIC | كل host_aliases جوار الـ host |
| 3 | `BRIDGE` | يوجد peer `bridge=true` | يقفز عبر subnet مختلف |
| 4 | `SINGLE_HOP_RELAY` | top-8 proxies من load_balancer | قفزة واحدة |
| 5 | `MULTI_HOP_RELAY` | top-4 proxies recursive | حتى 4 hops × 8 proxies |
| 6 | `REVERSE_TUNNEL` | `HELEN_RENDEZVOUS_HOST` set | للـ peers خلف NAT |
| 7 | `HOLE_PUNCH` | NAT detected | UDP P2P |
| 8 | `FEDERATION` | partition / cross-cluster | HMAC-signed |
| 9 | `CACHED_FALLBACK` | peer_registry cache مختلف عن الحالي | last-known-good |
| 10 | `RENDEZVOUS_HINT` | rendezvous server يقترح | URL خارجي |

---

## 3. خوارزمية اختيار أفضل مسار

10 عوامل تُجمَّع في معادلة واحدة:

| العامل | المصدر | المدى |
|---|---|---|
| Latency | `path_health.latency_score` | 0..2 |
| Packet loss (proxy) | `consecutive_failures` | 0..1 |
| Bandwidth | `bandwidth_probe.get` | 0..1 |
| Jitter (مشتق) | latency variance | implicit في latency_score |
| Hop count | `len(hops)` | factor 1.0..0.2 |
| NAT type | route class mapping | 0..1 |
| Trust score | `trust_score.get_score(first_hop)` | 0..1 |
| Node load | `compute_headroom(load)` | 0..1 |
| Route age | `time - last_success_at` | 0..1 |
| Security level | route class (HMAC vs raw) | 0.7 / 1.0 |

### شروط الرفض الفوري (score = 0)
1. `is_in_cooldown()` — المسار فشل خلال آخر 30s
2. `trust < 0.10` — peer مُحجور
3. `phi >= 8.0` — phi accrual يقول "ميت"
4. أي استثناء أثناء الحساب

---

## 4. Route Scoring — المعادلة الكاملة

```
raw = w_lat × (latency_score / 2)
    + w_loss × loss_score
    + w_bw × bw_score
    + w_trust × trust_score
    + w_load × load_score
    + w_hops × hops_factor(hop_count)
    + w_age × age_score
    + w_sec × security_level
    + w_nat × nat_friendliness

final = raw × class_floor(route_type)
```

### Weights (مجموع = 1.0)

| العامل | الوزن |
|---|---|
| `w_lat` | 0.25 |
| `w_loss` | 0.15 |
| `w_bw` | 0.10 |
| `w_trust` | 0.15 |
| `w_load` | 0.10 |
| `w_hops` | 0.10 |
| `w_age` | 0.05 |
| `w_sec` | 0.05 |
| `w_nat` | 0.05 |

### Class Floors (أسبقية ثابتة لنوع المسار)

```
DIRECT            → 1.00
LAN_ALIAS         → 0.95
BRIDGE            → 0.85
SINGLE_HOP_RELAY  → 0.75
MULTI_HOP_RELAY   → 0.65
FEDERATION        → 0.55
CACHED_FALLBACK   → 0.50
REVERSE_TUNNEL    → 0.40
HOLE_PUNCH        → 0.30
RENDEZVOUS_HINT   → 0.25
```

⇒ DIRECT بدرجة سيئة لا يَخسر أمام RENDEZVOUS_HINT بدرجة جيدة.

### تفضيل الاحتياطي
يتم عند: `score(primary) == 0` أو `attempt[0] returned 5xx/timeout`.

---

## 5. آلية Failover

```
attempt 0 (top route)         ──ok──▶ return
        │ fail (status 5xx / timeout)
        ▼
attempt 1 (2nd best)          ──ok──▶ return
        │ fail
        ▼
attempt 2 (3rd best)          ──ok──▶ return
        │ fail
        ▼
enqueue dead_letter_service ⟹ background retry exponential 2s..60s
```

كل فشل يستدعي `_record_outcome(route, success=False)`:
1. `consecutive_failures += 1`
2. `failed_until = now + 30s`
3. لن يُختار مرة أخرى حتى انقضاء الـ cooldown

عند نجاح المسار بعد فشل سابق:
1. `consecutive_failures = 0`
2. `failed_until = 0`
3. يعود مسارًا full-priority

### استخدام parallel
موجود كـ option في `cluster_mesh.relay_request` (يحاول 8 proxies بالتوازي خلال recursive). الـ multipath_router يستخدم sequential لأن RTO الـadaptive يجعل التتابع أرخص من race الكامل.

---

## 6. Multi-hop Routing

| Hop count | كيف يعمل |
|---|---|
| 1-hop | proxy → target مباشر |
| 2-hop | proxy_A → proxy_B → target (recursive) |
| 3-hop | proxy_A → proxy_B → proxy_C → target |
| 4-hop | السلسلة الكاملة (max default) |

### منع الحلقات (loop prevention)
- `seen_proxies: set[str]` — كل قفزة تضيف نفسها
- proxy موجود في `seen_proxies` ⇒ يُستبعد فورًا
- `hops_remaining` يبدأ بـ 4 ويتناقص — يضمن termination مهما حدث

### TTL
1. على مستوى الـ envelope: `MAX_HOPS_PRODUCTION = 64`
2. على مستوى الـ relay: `hops_remaining = 4`
3. على مستوى الـ HTTP: `adaptive_timeout` per-peer RTO

### اختيار relays موثوقة
الـ`load_balancer.rank_proxies(top_k=8)` يستثني:
- المسارات في cooldown
- peers بـ `trust < 0.10`
- peers بـ `phi >= 8.0`
- peers مع backpressure REJECTED

---

## 7. Load Balancing

| الخوارزمية | الاستخدام |
|---|---|
| Round-robin | غير مستخدم (deterministic = bad cache) |
| **Weighted** | الافتراضي — `load_balancer.rank_proxies` |
| **Adaptive** | weights تتحدّث live حسب path_health/trust/headroom |
| **Parallel probing** | `latency_prober` كل 30s |
| **Split traffic** | hop expansion: top-8 proxies ⇒ يقسم على عدة مسارات |
| **Priority queues** | `essential` flag في `backpressure.decide` |

### معادلة proxy_weight
```
weight = (W_LAT × latency_norm
        + W_TRUST × trust
        + W_HEAD × headroom
        + W_CAP × capacity_norm)
        × (1.10 if bridge else 1.0)
```

---

## 8. Route Discovery

| الآلية | كيف |
|---|---|
| **Active probing** | `latency_prober` HEAD على cluster/info |
| **Passive learning** | `path_health.record_*` من traffic حقيقي |
| **Gossip exchange** | `peer_registry` يأخذ peers من peers |
| **DHT lookup** | `dht_kademlia` للبحث عن user_id → server_id |
| **Peer cache** | `data/peers_cache.json` على القرص |
| **Rendezvous hints** | `HELEN_RENDEZVOUS_HOST` env |

---

## 9. طبقة الأمان

| الميزة | التنفيذ |
|---|---|
| HMAC على كل route request | `federation_auth.sign_request` |
| Public/private identity (option) | Ed25519 (ليس مفعّل بعد للـ LAN) |
| Session encryption | TLS 1.3 ممكن، HMAC كافٍ على LAN |
| Replay protection | nonce cache + timestamp window 60s |
| Nonce + timestamp | في `X-Federation-Timestamp` |
| Signed relay chain | كل hop يعيد توقيع الطلب |
| Trust score per relay | `trust_score.get_score(first_hop)` |

---

## 10. إدارة الحالة

| الجدول | الموقع | المحتوى |
|---|---|---|
| **Route table** | `RouteTable` singleton | `(target, type, hops) → Route` |
| **Peer table** | `node_registry._nodes` | server_id → Node |
| **Relay table** | `peer_registry._peers` | + host_aliases / bridge |
| **Failure history** | `Route.consecutive_failures` | + `failed_until` |
| **Latency histogram** | `path_health._health` | EWMA + samples |
| **Retry queue** | `dead_letter_service` | persistent JSON |
| **Circuit breaker** | per-peer in `cluster_mesh` | 5 fail → open 30s |

---

## 11. مثال عملي: 10 PCs + 10 Servers + 10 Routers + 3 Subnets + NAT

```
                     INTERNET
                        │
        ┌───────────────┼───────────────┐
        │               │               │
   ┌────┴───┐      ┌────┴───┐      ┌────┴───┐
   │NAT-A   │      │NAT-B   │      │NAT-C   │
   │symmetric│     │full-cone│     │restricted│
   └────┬───┘      └────┬───┘      └────┬───┘
        │               │               │
   192.168.1.0/24  10.0.0.0/24    172.16.0.0/24
        │               │               │
   ┌────┴────┐     ┌────┴────┐     ┌────┴────┐
   │S1-S4    │     │S5-S7    │     │S8-S10   │
   │C1-C4    │     │C5-C7    │     │C8-C10   │
   └─────────┘     └─────────┘     └─────────┘
        │               │               │
        └─── S4 (USB-tether) ─── S7 (fiber) ───
             bridge=true       bridge=true
```

### كيف يُختار المسار من C1 (Server S1) إلى C9 (Server S9)؟

```
1. discover_routes(target=S9):
   - DIRECT: S1 → S9 host (NAT يمنع)
   - LAN_ALIAS: ولا host_alias مرئي
   - BRIDGE: S4 (يصل subnet B), S7 (يصل subnet C)
   - SINGLE_HOP_RELAY: top-8 من load_balancer
   - MULTI_HOP_RELAY: S4 → S7 → S9

2. select_strategy():
   - DIRECT eligible (لا backpressure)
   - BRIDGE eligible
   - MULTI_HOP_RELAY eligible

3. score_route لكل candidate:
   DIRECT (S1→S9):     score=0.0 (phi accrual high — لم يجاوب)
   BRIDGE (S1→S7→S9):  score=0.71 (latency=0.6, trust=0.95, hops=0.8)
   BRIDGE (S1→S4→S9):  score=0.65 (latency=0.5, S4→S9 indirect)
   MULTI (S1→S4→S7→S9): score=0.55 (hops=0.6 penalty)

4. attempt 0 = BRIDGE via S7 ⇒ نجح ⇒ return
```

---

## 12. حساب عدد المسارات

```
Direct paths:                  D = 1
1-hop relay paths per pair:    R₁ = K
2-hop relay paths per pair:    R₂ = K × (K-1)
3-hop relay paths per pair:    R₃ = K × (K-1) × (K-2)
4-hop relay paths per pair:    R₄ = K × (K-1) × (K-2) × (K-3)

Total per pair: P = D + R₁ + R₂ + R₃ + R₄
```

### بـ K=8 (proxies/hop):
```
D  = 1
R₁ = 8
R₂ = 8 × 7 = 56
R₃ = 8 × 7 × 6 = 336
R₄ = 8 × 7 × 6 × 5 = 1,680
─────────────────────────
P  = 2,081 مسار محتمل لكل زوج (A,B)
```

### في cluster من 10 سيرفرات:
```
عدد الأزواج: C(10,2) = 45
المسارات الإجمالية: 45 × 2,081 = 93,645
```

### في cluster من 1,000 سيرفر:
```
عدد الأزواج: C(1000,2) = 499,500
المسارات الإجمالية: 499,500 × 2,081 ≈ 1.04 مليار
```

---

## 13. Pseudo-code

### `discover_routes(target_id)`
```python
async def discover_routes(target_id):
    target = registry.get(target_id)
    routes = []
    routes.append(Route(DIRECT, hops=[target_id], host=target.host))
    for alias in target.host_aliases:
        routes.append(Route(LAN_ALIAS, hops=[target_id], host=alias))
    for bridge in registry.bridges():
        routes.append(Route(BRIDGE, hops=[bridge.id, target_id]))
    for proxy in load_balancer.rank_proxies(top_k=8):
        routes.append(Route(SINGLE_HOP_RELAY, hops=[proxy.id, target_id]))
    for rt in [FEDERATION, REVERSE_TUNNEL, HOLE_PUNCH, RENDEZVOUS_HINT]:
        routes.append(Route(rt, hops=[target_id]))
    return routes
```

### `score_route(route)`
```python
def score_route(r):
    if r.is_in_cooldown(): return 0
    if trust(r.first_hop) < 0.10: return 0
    if phi(r.first_hop) >= 8.0: return 0
    raw = sum(W[k] * factor(r, k) for k in WEIGHTS)
    return raw * CLASS_FLOOR[r.route_type]
```

### `select_best_route(target_id, k=3)`
```python
async def select_best_route(target_id, k=3):
    routes = await discover_routes(target_id)
    eligible = filter(lambda r: r.route_type in select_strategy(), routes)
    scored = [(score_route(r), r) for r in eligible]
    scored = [(s, r) for s, r in scored if s > 0]
    scored.sort(reverse=True)
    return [r for _, r in scored[:k]]
```

### `failover(target_id, request)`
```python
async def failover(target_id, request, max_attempts=3):
    routes = await select_best_route(target_id, k=max_attempts)
    for r in routes:
        try:
            response = await send_via_route(r, request)
            if 200 <= response.status < 300:
                record_success(r)
                return response
            record_failure(r)
        except (TimeoutError, ConnectionError):
            record_failure(r)
    await dead_letter_queue.enqueue(target_id, request)
    return None
```

### `relay_chain(target, hops_remaining, seen_proxies)`
```python
async def relay_chain(target, hops_remaining=4, seen_proxies=None):
    seen_proxies = seen_proxies or set()
    if try_direct(target): return ok
    if hops_remaining <= 0: return error("hops_exhausted")

    proxies = pick_proxies(top_k=8, exclude=seen_proxies)
    for proxy in proxies:
        seen_proxies.add(proxy.id)
        result = await proxy.relay(target, hops_remaining-1, seen_proxies)
        if result.ok: return result
    return error("all_paths_failed")
```

### `prevent_loop(seen_proxies, candidate)`
```python
def prevent_loop(seen_proxies, candidate):
    return candidate.id not in seen_proxies
```

### `update_route_metrics(route, success, latency_ms)`
```python
def update_route_metrics(route, success, latency_ms):
    now = time.time()
    route.last_used_at = now
    if success:
        route.last_success_at = now
        route.consecutive_failures = 0
        route.failed_until = 0
        path_health.record_success(route.first_host, route.first_port, latency_ms)
    else:
        route.consecutive_failures += 1
        route.failed_until = now + COOLDOWN_AFTER_FAIL_SEC
        path_health.record_failure(route.first_host, route.first_port)
```

---

## 14. مخطط ASCII لمسارات الاتصال

```
                              C1 (PC)
                               │
                               ▼
                          ┌──────────┐
                          │   S1     │ Server (Helen-Server)
                          │ (origin) │
                          └─────┬────┘
                                │
     ┌──────────────┬───────────┼───────────┬───────────────┐
     │              │           │           │               │
     ▼              ▼           ▼           ▼               ▼
  DIRECT       LAN_ALIAS    BRIDGE     1-HOP_RELAY      MULTI_HOP_RELAY
  (S1→S9      (S1→S9.eth1) (S1→S4→S9) (S1→S6→S9)      (S1→S4→S7→S9)
   primary)
     │              │           │           │               │
     └──────────────┴───────────┼───────────┴───────────────┘
                                │
                                ▼
                          ┌──────────┐
                          │   S9     │ target server
                          │ (target) │
                          └──────┬───┘
                                 │
                                 ▼
                                C9 (PC)


  Each Route in the Route Table:
  ┌────────────────────────────────────────────────────┐
  │ Route                                              │
  │   target_node_id:        S9                        │
  │   route_type:            BRIDGE                    │
  │   hops:                  [S4, S9]                  │
  │   first_host:            S4.host                   │
  │   first_port:            3000                      │
  │   score:                 0.71                      │
  │   last_score_at:         t-2s                      │
  │   last_used_at:          t-1s                      │
  │   last_success_at:       t-1s                      │
  │   consecutive_failures:  0                         │
  │   failed_until:          0                         │
  └────────────────────────────────────────────────────┘
                                │
                                ▼
              [scoring + cooldown + trust + phi]
                                │
                                ▼
                  send_via_route → relay_request
                                │
                                ▼
                  on success: record_success
                  on failure: record_failure → cooldown 30s
                              → next attempt
                              → if all 3 fail: dead_letter
```

---

## 15. الجدول النهائي

| النوع | متى يستخدم | الميزة | العيب | الأولوية | شرط الفشل |
|---|---|---|---|---|---|
| **DIRECT** | LAN healthy، الـ target متصل مباشر | أسرع، latency أدنى | لا يعمل عبر NAT | 1.00 | timeout > RTO |
| **LAN_ALIAS** | peer multi-NIC | يتجاوز interface واحد فاشل | يتطلب host_aliases | 0.95 | جميع aliases فشل |
| **BRIDGE** | راوتر مختلف، peer له bridge=true | يعبر subnets | latency أعلى | 0.85 | bridge ميت |
| **SINGLE_HOP_RELAY** | لا direct، يوجد proxy | بسيط | hop واحد إضافي | 0.75 | proxy فاشل |
| **MULTI_HOP_RELAY** | لا proxy واحد يصل | يعبر مهما كانت التقسيمات | latency عالية | 0.65 | hops_remaining=0 |
| **FEDERATION** | cross-cluster | HMAC-signed، آمن | يتطلب cluster_id match | 0.55 | unauthenticated |
| **CACHED_FALLBACK** | last-known-good مختلف | يعمل بعد network change | معلومات قديمة | 0.50 | cached host expired |
| **REVERSE_TUNNEL** | target خلف NAT | يتجاوز firewall | latency 50-200ms | 0.40 | rendezvous unreachable |
| **HOLE_PUNCH** | كلا الطرفين خلف NAT | P2P بدون relay | symmetric NAT يفشل | 0.30 | NAT type symmetric |
| **RENDEZVOUS_HINT** | لا يوجد route آخر | آخر ملاذ | يتطلب خدمة خارجية | 0.25 | rendezvous رفض |

---

## 📌 التشغيل التلقائي (auto mode)

النظام يعمل **حسب الظروف** بدون أي تدخل:

```python
# تلقائي في startup
from app.services.multipath_router import start_multipath_router
start_multipath_router()      # background loop يحدّث route table كل 30s

# الاستخدام:
status, body, headers = await multipath_router.send(
    target_node_id=peer_id,
    method="POST", path="/api/x", body={"hello": "world"}
)
```

`select_strategy()` يلتقط الظروف ويغيّر سلوكه آليًا:

| الظرف | التأثير على الاستراتيجية |
|---|---|
| `backpressure == REJECTED` | يستبعد DIRECT/LAN_ALIAS — يبعث للـ peers |
| `partition_state.is_majority == False` | يضيف FEDERATION + REVERSE_TUNNEL |
| `HELEN_RENDEZVOUS_HOST set` | يفعّل REVERSE_TUNNEL + RENDEZVOUS_HINT |
| `phi(peer) >= 8` | يرفض كل route عبره |
| `trust(peer) < 0.10` | يرفض كل route عبره |

---

*وثيقة معمارية حية — كل قسم يقابل كود فعلي في `app/services/multipath_router.py`.*
