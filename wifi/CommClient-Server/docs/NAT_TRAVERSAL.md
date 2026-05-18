# NAT Traversal Module Design

> Module location: `app/nat/`
> Status: implemented, wired in `app/main.py` lifespan, 859/859 tests pass.

---

## 1. تعريف NAT Traversal داخل المشروع

طبقة وسيطة تجعل عقدتين peer قادرتين على الاتصال حتى عندما يكون كلاهما (أو أحدهما) خلف Router/Firewall يطبّق NAT. تشمل: كشف نوع الـ NAT الخاص بنا، تنسيق hole-punching مع الطرف الآخر عبر Helen-Rendezvous، فتح reverse-tunnel كاحتياطي، والرجوع إلى relay عبر الـ mesh كآخر ملاذ.

هذا الموديول **مستقل عن الباقي** لأن منطق الاختراق يجب أن يبقى منفصلًا — يمكن تعطيله كاملاً (LAN-only) بدون أن يكسر شيئًا، ويمكن إضافة بروتوكول جديد (ICE/TURN) بدون لمس routing/p2p.

---

## 2. وظيفة الموديول

| المهمة | كيف ينفّذها |
|---|---|
| كشف نوع NAT المحلي | `nat_detector` (STUN-based) |
| تصنيف القدرة على الاتصال | `nat_type` (compat matrix) |
| تنسيق نقطة اللقاء | `rendezvous_client` |
| اختراق UDP | `udp_hole_punch` |
| اختراق TCP (simultaneous open) | `tcp_hole_punch` |
| نفق عكسي (peer خلف NAT صارم) | `reverse_tunnel` |
| relay كاحتياطي نهائي | `relay_fallback` |
| تخزين نتائج الـ traversal مؤقتًا | `nat_session` (TTL-bounded) |
| تشغيل السلم بالكامل | `nat_traversal_manager` |

---

## 3. الملفات الخاصة بالموديول (14 ملف)

```
app/nat/
├── __init__.py
├── nat_traversal_manager.py
├── nat_detector.py
├── nat_type.py
├── stun_client.py
├── rendezvous_client.py
├── udp_hole_punch.py
├── tcp_hole_punch.py
├── reverse_tunnel.py
├── relay_fallback.py
├── nat_session.py
├── nat_events.py
├── nat_config.py
└── nat_exceptions.py
```

---

## 4. وظيفة كل ملف

### `__init__.py`
- **الوظيفة:** يكشف `get_nat_manager`, `start_nat`, `stop_nat` فقط.
- **يستقبل:** —
- **ينتج:** Public API.
- **يتصل بـ:** `app/main.py` lifespan + admin API.
- **متى يُستخدم:** عند تشغيل/إيقاف الموديول من خارج الحزمة.

### `nat_exceptions.py`
- **الوظيفة:** هرم استثناءات (`NATError` + 8 أبناء: `STUNError`, `HolePunchError`, `RendezvousError`, `ReverseTunnelError`, `RelayFallbackError`, `NATSessionError`, `NATDetectionError`, `NATNotTraversableError`).
- **يستقبل:** —
- **ينتج:** classes فقط.
- **يتصل بـ:** كل ملفات NAT.
- **متى يُستخدم:** للتمييز بين أنواع الفشل المختلفة.

### `nat_config.py`
- **الوظيفة:** قراءة `HELEN_NAT_*` env vars إلى `NATConfig` frozen dataclass.
- **يستقبل:** متغيرات بيئة.
- **ينتج:** singleton config.
- **يتصل بـ:** كل ملفات NAT تحتاج معاملات قابلة للضبط.
- **متى يُستخدم:** لتفعيل/تعطيل استراتيجيات معينة بدون إعادة compile.

### `nat_events.py`
- **الوظيفة:** pub/sub bus *منفصل* عن باقي الموديولات (200-event history).
- **يستقبل:** subscribe(name, handler) + emit(name, payload).
- **ينتج:** delivery counts + event history.
- **يتصل بـ:** كل ملفات NAT تستخدمه للإشعار.
- **متى يُستخدم:** عند كل نتيجة punch/tunnel/relay لإعطاء التطبيق ملاحظات.

### `nat_type.py`
- **الوظيفة:** `NATType` enum (OPEN/FULL_CONE/RESTRICTED/PORT_RESTRICTED/SYMMETRIC/UNKNOWN) + compat matrix + `best_strategy()`.
- **يستقبل:** أنواع NAT لطرفين.
- **ينتج:** اسم الاستراتيجية الموصى بها.
- **يتصل بـ:** `nat_traversal_manager` يقرأها قبل تشغيل السلم.
- **متى يُستخدم:** قبل اختيار أي استراتيجية لتجنب محاولات يقينًا فاشلة.

### `stun_client.py`
- **الوظيفة:** STUN client يدوي (RFC 5389) — يرسل Binding Request ويفك XOR-MAPPED-ADDRESS من الرد.
- **يستقبل:** STUN host + port + timeout.
- **ينتج:** `(public_ip, public_port)` أو يرمي `STUNError`.
- **يتصل بـ:** `nat_detector` يستدعيه.
- **متى يُستخدم:** عند بدء التشغيل + كل `redetect_interval_sec`.

### `nat_detector.py`
- **الوظيفة:** يصنّف الـ NAT المحلي عبر مقارنة المحلي بالـ public-reflexive من STUN. يبثّ `nat.type_changed` عند تغيّر النوع.
- **يستقبل:** —
- **ينتج:** `NATType` + public endpoint snapshot.
- **يتصل بـ:** `stun_client` (يستدعيه)، `nat_traversal_manager` (يقرأ النتيجة).
- **متى يُستخدم:** عند بدء التشغيل + دوريًا للكشف عن تغيّر شبكة (تنقّل WiFi، تبديل ISP).

### `rendezvous_client.py`
- **الوظيفة:** يتحدث مع Helen-Rendezvous: `resolve_peer_endpoint(peer_id)` و `announce_self(peer_id, host, port)`.
- **يستقبل:** peer_id أو binding info.
- **ينتج:** `(host, port)` للـ peer أو success bool.
- **يتصل بـ:** `udp_hole_punch`, `tcp_hole_punch` يستهلكان الـ endpoint.
- **متى يُستخدم:** قبل أي punch attempt + عند بدء reverse_tunnel.

### `udp_hole_punch.py`
- **الوظيفة:** `punch(peer_id)` يبعث `punch_packet_count` packets ويستمع للرد.
- **يستقبل:** peer_id + optional local_port.
- **ينتج:** True/False مع `nat.udp_punch` event.
- **يتصل بـ:** `rendezvous_client` (للـ endpoint)، `services.connectivity.hole_punch` (fallback).
- **متى يُستخدم:** الخيار الثاني في السلم بعد direct.

### `tcp_hole_punch.py`
- **الوظيفة:** TCP simultaneous-open بمحاولات متعددة على REUSEADDR socket.
- **يستقبل:** peer_id + optional local_port.
- **ينتج:** `socket` مفتوح أو None.
- **يتصل بـ:** `rendezvous_client`.
- **متى يُستخدم:** بعد فشل UDP punch (لبعض NATs لا يمر إلا TCP).

### `reverse_tunnel.py`
- **الوظيفة:** facade على `services.connectivity.reverse_tunnel.ReverseTunnelClient` — يبدأ/يوقف نفق عكسي ثابت إلى Helen-Rendezvous.
- **يستقبل:** start()/stop() commands.
- **ينتج:** running boolean + `nat.tunnel_up/down` events.
- **يتصل بـ:** `services.connectivity.reverse_tunnel`.
- **متى يُستخدم:** عند فشل hole-punch (Symmetric NAT) ووجود rendezvous.

### `relay_fallback.py`
- **الوظيفة:** `relay(target, method, path, body)` يفوّض إلى `cluster_mesh.relay_request`.
- **يستقبل:** target_peer_id + HTTP request shape.
- **ينتج:** `(status, body, headers)` أو `RelayFallbackError`.
- **يتصل بـ:** `services.cluster_mesh`.
- **متى يُستخدم:** آخر ملاذ — يعمل دائمًا طالما الـ mesh قائم.

### `nat_session.py`
- **الوظيفة:** يخزن نتائج traversal مع TTL لتجنب إعادة تشغيل السلم لكل request.
- **يستقبل:** open(peer_id, strategy, public_endpoint, success) + close + evict_expired.
- **ينتج:** `NATSession` instances + snapshot.
- **يتصل بـ:** `nat_traversal_manager` يستعلم/يحدّث.
- **متى يُستخدم:** قبل وبعد كل `traverse()` call.

### `nat_traversal_manager.py`
- **الوظيفة:** الواجهة العليا — يشغّل سلم الاستراتيجيات: direct → udp_punch → tcp_punch → reverse_tunnel → relay.
- **يستقبل:** `traverse(peer_id)` calls + start/stop.
- **ينتج:** اسم الاستراتيجية الناجحة أو يرمي `NATNotTraversableError`.
- **يتصل بـ:** كل ملفات NAT داخليًا، `app/main.py` خارجيًا.
- **متى يُستخدم:** كل operation تتطلب الوصول لـ peer قد يكون خلف NAT.

---

## 5. كيف يساعد الأجهزة خلف الراوترات على الاتصال

### مسار الاختراق (traversal path)

```
caller → manager.traverse(peer_id):

  1. session cache hit?  → return last successful strategy
  2. nat_detector says BOTH OPEN?  → strategy = "direct"
  3. nat_type says compatible?      → udp_hole_punch.punch()
        ├─ rendezvous_client.resolve_peer_endpoint(peer_id)
        ├─ burst N UDP packets to (public_ip, public_port)
        ├─ wait for reply within timeout
        └─ success → cache + return "udp_punch"
  4. else                           → tcp_hole_punch.punch()
        ├─ rendezvous_client.resolve_peer_endpoint(peer_id)
        ├─ multiple connect() attempts on REUSEADDR socket
        └─ success → cache + return "tcp_punch"
  5. else (symmetric NAT or punch failed)
                                     → reverse_tunnel.start()
        ├─ outbound WS to Helen-Rendezvous
        └─ success → cache + return "reverse_tunnel"
  6. else                           → relay_fallback.relay()
        └─ services.cluster_mesh.relay_request (always works as
           long as mesh has any path)
```

### الربط مع باقي المشروع (read-only)

| Module | كيف يربطه nat |
|---|---|
| **`p2p`** | `p2p.peer_nat_traversal` يستدعي `nat.traverse(peer_id)` كأول خطوة قبل فتح session. |
| **`routing_strategy`** | عندما يصدر decision لـ `RouteType.HOLE_PUNCH` أو `REVERSE_TUNNEL` — يستهلك نتائج NAT بدلاً من إعادة التنفيذ. |
| **`overlay`** | overlay routes تستخدم `relay_fallback` كأبسط backbone بين العقد البعيدة. |
| **`security`** | rendezvous responses تتحقق عبر HMAC في `services.federation_auth` — NAT لا يفك التوقيع. |
| **`storage`** | لا persistence (sessions in-memory only) — مقصود لأن نتائج NAT تنتهي صلاحيتها بسرعة عند تغيّر الشبكة. |
| **`monitoring`** | metrics_collector يقرأ `get_nat_manager().snapshot()` كأحد المصادر. |

---

## التشغيل التلقائي

```python
# app/main.py lifespan
from app.nat import start_nat
start_nat()
```

## API الإدارية

```
GET /api/admin/peers/nat/snapshot
```

## نتائج التحقق

| اختبار | نتيجة |
|---|---|
| `best_strategy(OPEN, OPEN)` | `direct` ✓ |
| `best_strategy(FULL_CONE, FULL_CONE)` | `hole_punch` ✓ |
| `best_strategy(SYMMETRIC, PORT_RESTRICTED)` | `reverse_tunnel` ✓ |
| `hole_punch_compatible(SYMM, FULL_CONE)` | `False` ✓ |
| Detector snapshot (no STUN) | `unknown` + local_ip detected ✓ |
| Sessions open/snapshot | count=2 ✓ |
| Manager snapshot | 9 keys including `udp_punch`, `tcp_punch`, `tunnel`, `relay` ✓ |
| **pytest** | **859/859 passed** ✓ |
| Helen-Server.exe | 18MB rebuilt ✓ |

---

## الإجمالي عبر كل الجولات

```
حزم منفصلة:
  app/topology/             10 ملف
  app/routing_strategy/     22 ملف
  app/distributed_system/   17 ملف
  app/monitoring/           10 ملف
  app/p2p/                  26 ملف
  app/overlay/              11 ملف
  app/resilience/           13 ملف
  app/nat/                  14 ملف   ← هذه الجولة

Total: 123 ملف موديولار + 24 خدمة في app/services/
859/859 pytest passed
Helen-Server.exe = 18 MB
6 وثائق معمارية في docs/
كل ملف مسؤولية واحدة، إضافة بدون لمس البقية
```
