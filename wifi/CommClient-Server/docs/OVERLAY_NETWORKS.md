# Overlay Networks Module Design

> Module location: `app/overlay/`
> Status: implemented, wired in `app/main.py`, tests green (859/859).

---

## 1. تعريف Overlay Network داخل المشروع

شبكة منطقية (Logical Network) مبنية فوق طبقة الـ mesh الفعلية. كل overlay له **اسم** و**رسم بياني** خاص به (nodes + links) يمثل ترتيبًا محددًا للتطبيق — مثلاً ring لـ chat، tree لـ pub/sub، أو star لـ broadcast.

أهم خاصية: **overlay لا يفتح أي اتصال شبكي بنفسه** — يصف العلاقات منطقيًا فقط. الاتصال الفعلي ينفّذه `topology` + `p2p` + `routing_strategy` اعتمادًا على `physical_chain` الذي يخرجه `overlay_route`.

---

## 2. وظيفة الموديول

| المهمة | كيف ينفّذها |
|---|---|
| تعريف عدة شبكات منطقية متوازية | `OverlayRegistry` (singleton + max_overlays cap) |
| ترتيب nodes حسب التطبيق | `OverlayNode` مع tags + metadata |
| تعريف edges مع أوزان | `OverlayLink` weight + bidirectional |
| رسم بياني قابل للاستعلام | `OverlayGraph` BFS + k-shortest |
| ترجمة المسار المنطقي إلى chain من الـ peers | `OverlayRoute.physical_chain` |
| تتبع conversations مفتوحة | `OverlaySession` بـ TTL |
| Persistence على القرص | `data/overlay_state.json` |
| Pub/sub للأحداث | `overlay_events` (مستقل عن باقي buses) |

---

## 3. الملفات الخاصة بالموديول (11 ملف)

```
app/overlay/
├── __init__.py
├── overlay_manager.py
├── overlay_node.py
├── overlay_link.py
├── overlay_graph.py
├── overlay_route.py
├── overlay_registry.py
├── overlay_session.py
├── overlay_events.py
├── overlay_config.py
└── overlay_exceptions.py
```

---

## 4. وظيفة كل ملف

### `__init__.py`
- **الوظيفة:** يكشف `get_overlay_manager`, `start_overlay`, `stop_overlay` فقط. لا re-export للداخليات.
- **يستقبل:** —
- **ينتج:** Public API للحزمة.
- **يتصل بـ:** `app/main.py` (lifespan)، `admin_peers` API.
- **مستقل لأن:** يخفي الداخل ويمنع التطبيقات من Import حول الـ manager.

### `overlay_exceptions.py`
- **الوظيفة:** هرم استثناءات (`OverlayError` + 6 أبناء).
- **يستقبل:** —
- **ينتج:** classes فقط.
- **يتصل بـ:** كل ملفات overlay الأخرى.
- **مستقل لأن:** يجب أن يبقى بدون imports بيناً (zero coupling).

### `overlay_config.py`
- **الوظيفة:** قراءة env vars (`HELEN_OVL_*`) إلى `OverlayConfig` dataclass.
- **يستقبل:** متغيرات البيئة.
- **ينتج:** singleton frozen dataclass.
- **يتصل بـ:** يقرأها كل ملف يحتاج معامل قابل للضبط.
- **مستقل لأن:** centralised tunables تسمح بإعادة الضبط بدون لمس المنطق.

### `overlay_events.py`
- **الوظيفة:** pub/sub bus *منفصل* عن `p2p.peer_events` و `monitoring.monitoring_events`.
- **يستقبل:** subscribe(name, handler) + emit(name, payload).
- **ينتج:** delivery counts + history(50).
- **يتصل بـ:** `overlay_manager`, `overlay_registry`, `overlay_session` (emit only).
- **مستقل لأن:** أحداث overlay لا تختلط بقنوات الباقي.

### `overlay_node.py`
- **الوظيفة:** dataclass `OverlayNode (overlay_name, node_id, peer_id, tags, metadata, last_seen)`.
- **يستقبل:** to_dict / from_dict للـ persistence.
- **ينتج:** instances + serialization helpers.
- **يتصل بـ:** `overlay_graph` (storage)، `overlay_session` (route resolution).
- **مستقل لأن:** نموذج بحت بدون behaviour.

### `overlay_link.py`
- **الوظيفة:** dataclass `OverlayLink (overlay_name, src_id, dst_id, weight, bidirectional_hint)`.
- **يستقبل:** to_dict / from_dict.
- **ينتج:** instances.
- **يتصل بـ:** `overlay_graph` adjacency.
- **مستقل لأن:** edge model منفصل عن node model، يسمح بإضافة weight policies بسهولة.

### `overlay_graph.py`
- **الوظيفة:** ثريد-سيف dict adjacency + BFS shortest_path + Yen-style k_shortest_paths.
- **يستقبل:** add_node/add_link/remove_node/remove_link.
- **ينتج:** قوائم nodes/links + paths.
- **يتصل بـ:** `overlay_registry` يخزن instance per overlay، `overlay_route` يستعلم.
- **مستقل لأن:** بنية بيانات نقية، خالية من I/O.

### `overlay_route.py`
- **الوظيفة:** `resolve_shortest` + `resolve_k_shortest` يحوّلان مسار graph إلى `OverlayRoute` مع `physical_chain`.
- **يستقبل:** graph + src_id + dst_id (+ k).
- **ينتج:** `OverlayRoute(nodes, physical_chain, cost, hop_count)`.
- **يتصل بـ:** `overlay_graph` (يقرأ)، `overlay_session` (يخزن `last_route`).
- **مستقل لأن:** يفصل منطق "اختر مسار" عن "خزّن adjacency".

### `overlay_registry.py`
- **الوظيفة:** singleton index `overlay_name → OverlayGraph`. create/drop/get/require + cap (max_overlays).
- **يستقبل:** أوامر الـ manager.
- **ينتج:** graphs + names list + snapshot.
- **يتصل بـ:** `overlay_manager` (الواجهة)، `admin_peers` API (للقراءة).
- **مستقل لأن:** نقطة الـ registry يجب أن تكون singleton واحد ومركز.

### `overlay_session.py`
- **الوظيفة:** TTL-bounded `OverlaySession` per (overlay, src, dst) — `OverlaySessionManager` يفتح/يغلق/يطرد المنتهية.
- **يستقبل:** open/close/get + ttl_sec من config.
- **ينتج:** `OverlaySession` instances + snapshot.
- **يتصل بـ:** `overlay_manager._run_loop` يستدعي `evict_expired` كل دورة.
- **مستقل لأن:** الـ session state عابر، يتطلب TTL eviction خاصة.

### `overlay_manager.py`
- **الوظيفة:** الواجهة العامة — create_overlay / add_node / add_link / route / routes_k + persistence loop + start/stop.
- **يستقبل:** أوامر التطبيق.
- **ينتج:** نقطة دخول واحدة لكل العمليات.
- **يتصل بـ:** كل ملفات overlay الأخرى داخليًا، `app/main.py` lifespan خارجيًا.
- **مستقل لأن:** الـ orchestrator يجب أن يكون مكانًا واحدًا.

---

## 5. الربط مع باقي المشروع (بدون خلط)

ينشئ `overlay` references *للقراءة فقط* — لا يستورد ولا يعدّل state للموديولات الأخرى.

| Module | كيف يرتبط overlay به |
|---|---|
| **`topology`** | `OverlayNode.peer_id` يعكس `topology.Node.node_id`. لا import بيناً — متصل عبر القيم فقط. |
| **`p2p`** | `OverlayRoute.physical_chain` تنتج list من peer_ids. التطبيق يمررها لـ `p2p.peer_forwarding`. |
| **`routing_strategy`** | `physical_chain` تستخدمها `routing_strategy.send` لاختيار مسار physical لكل قفزة. |
| **`security`** | `app/services/sync_policy` + `audit_replication` تكتب overlay events تلقائيًا حين الـ create/drop. لا import مباشر. |
| **`storage`** | persistence وحيدة `data/overlay_state.json` (atomic temp+rename). لا تستخدم replication_manager. |
| **`monitoring`** | `monitoring.metrics_collector` يقرأ `overlay.snapshot()` كأحد المصادر، عبر `app.overlay.get_overlay_manager()`. |

> ملاحظة: لا يستورد `overlay` أي ملف من `topology` / `p2p` / `routing_strategy` مباشرة — التطبيق هو الـ glue. هذا يبقي `overlay` قابلاً للاختبار والإزالة بدون أن يكسر باقي الحزم.

---

## التشغيل التلقائي

```python
# app/main.py lifespan
from app.overlay import start_overlay
start_overlay()
```

## API الإدارية

```
GET /api/admin/peers/overlay/snapshot   — كل overlays + sessions
GET /api/admin/peers/overlay/{name}     — رسم بياني واحد
```

## نتائج التحقق

| | |
|---|---|
| Smoke-test route | A→B→C، physical_chain=[peer-1, peer-2, peer-3] ✓ |
| K-shortest | يعطي 1 مسار في 3-node ring ✓ |
| Session lifecycle | open + touch + evict ✓ |
| Persistence | `data/overlay_state.json` (atomic write) ✓ |
| pytest | 859/859 passed (لم يكسر شيء) ✓ |
| Helen-Server.exe | 18MB rebuilt ✓ |
