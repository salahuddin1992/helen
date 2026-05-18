# Resilient Networking Module Design

> Module location: `app/resilience/`
> Status: implemented, wired in `app/main.py` lifespan, 859/859 tests pass.

---

## 1. تعريف Resilient Networking داخل المشروع

طبقة كشف فشل + استرداد ذاتي تجلس فوق primitives الـ mesh لتجعل الـ stack بأكمله يبقى قائمًا حين تموت العقد، تتقطع الروابط، أو تدخل بعض الأقسام في عزل (partition). هي **مستقلة لأن عمل recovery يجب أن يبقى منفصلًا عن منطق routing/topology** — حتى عند فشل أحد تلك الموديولات، تستمر هذه في تسجيل/تتبع/إعادة المحاولة.

---

## 2. وظيفة الموديول

| المهمة | كيف ينفّذها |
|---|---|
| كشف فشل العقد | `failure_detector` (phi accrual + custom probes) |
| تصنيف الفشل | `failure_classifier` (TRANSIENT/PERMANENT/NETWORK/SECURITY/OVERLOAD) |
| كسر الدوائر للأهداف الفاشلة | `circuit_breaker` (closed/open/half-open) |
| تحديد فترات backoff | `retry_policy` (exponential + jitter) |
| طابور إعادة محاولة دائم | `retry_queue` (persistent JSON-lines) |
| تجاوز الفشل بمسارات بديلة | `failover_manager` |
| اكتشاف الانهيار العام | `degraded_mode` (NORMAL/DEGRADED/EMERGENCY) |
| تشغيل أعمال الاسترداد | `recovery_manager` (event-driven) |
| توحيد الواجهة | `resilience_manager` |

---

## 3. الملفات الخاصة بالموديول (13 ملف)

```
app/resilience/
├── __init__.py
├── resilience_manager.py
├── failure_detector.py
├── failure_classifier.py
├── failover_manager.py
├── recovery_manager.py
├── retry_policy.py
├── retry_queue.py
├── circuit_breaker.py
├── degraded_mode.py
├── resilience_events.py
├── resilience_config.py
└── resilience_exceptions.py
```

---

## 4. وظيفة كل ملف

### `__init__.py`
- **الوظيفة:** يكشف `get_resilience_manager`, `start_resilience`, `stop_resilience` فقط.
- **يستقبل:** —
- **ينتج:** Public API.
- **يتصل بـ:** `app/main.py` lifespan + `admin_peers` API.
- **متى يُستخدم:** عند تشغيل/إيقاف الموديول من خارج الحزمة.

### `resilience_exceptions.py`
- **الوظيفة:** هرم استثناءات (`ResilienceError` + 6 أبناء: `CircuitOpenError`, `RetryExhaustedError`, `FailoverError`, إلخ).
- **يستقبل:** —
- **ينتج:** classes فقط.
- **يتصل بـ:** كل ملفات resilience.
- **متى يُستخدم:** عند الحاجة لتصنيف فشل بدلاً من رمي Exception عام.

### `resilience_config.py`
- **الوظيفة:** قراءة `HELEN_RES_*` env vars إلى `ResilienceConfig` frozen dataclass.
- **يستقبل:** متغيرات بيئة.
- **ينتج:** singleton config.
- **يتصل بـ:** كل ملفات resilience التي تحتاج معاملًا قابلاً للضبط.
- **متى يُستخدم:** كلما أردت تغيير threshold/cooldown/TTL بدون تعديل كود.

### `resilience_events.py`
- **الوظيفة:** pub/sub bus *منفصل* (200-event history) — أحداث resilience لا تختلط بقنوات الموديولات الأخرى.
- **يستقبل:** subscribe(name, handler) + emit(name, payload).
- **ينتج:** delivery counts + history.
- **يتصل بـ:** كل ملفات resilience تستخدمه للإشعار.
- **متى يُستخدم:** كلما حدث failover/breaker/recovery action.

### `failure_detector.py`
- **الوظيفة:** facade على phi_accrual + register_probe لاختبارات مخصصة (DB/disk/external).
- **يستقبل:** peer_id أو probe registration.
- **ينتج:** is_alive / phi value / probe results.
- **يتصل بـ:** `services/phi_accrual` (يقرأ).
- **متى يُستخدم:** قبل كل routing decision لتحديد إن كان الـ target حيًا.

### `failure_classifier.py`
- **الوظيفة:** تصنيف الـ exception أو الـ status code إلى `FailureKind`.
- **يستقبل:** `BaseException` أو `(status_code, body)`.
- **ينتج:** `FailureKind` + `is_retryable()` + `cooldown_multiplier()`.
- **يتصل بـ:** `failover_manager`, `retry_policy`, `retry_queue`.
- **متى يُستخدم:** فور وقوع فشل لتحديد المسار: retry / failover / abort.

### `retry_policy.py`
- **الوظيفة:** دوال نقية: `compute_delay(attempt, kind)` + `should_retry(attempt, kind)`.
- **يستقبل:** attempt index + FailureKind.
- **ينتج:** delay seconds + retry decision.
- **يتصل بـ:** `retry_queue`, `failover_manager`.
- **متى يُستخدم:** عند جدولة retry بعد فشل قابل للتجاوز.

### `retry_queue.py`
- **الوظيفة:** طابور دائم على القرص (`data/resilience_retry_queue.jsonl`) + dispatcher background loop.
- **يستقبل:** `enqueue(task_kind, payload, attempt, failure_kind)` + handlers مسجلة.
- **ينتج:** delayed execution + persistence + retry/exhaustion events.
- **يتصل بـ:** التطبيق يسجل handlers، `resilience_manager` يبدأ الـ loop.
- **متى يُستخدم:** عند فشل failover_manager في عثور target — تأجيل العملية.

### `circuit_breaker.py`
- **الوظيفة:** breaker per-target مع state machine (CLOSED/OPEN/HALF_OPEN).
- **يستقبل:** `record_success(target)` / `record_failure(target)` + `allow(target)`.
- **ينتج:** allow boolean + state events + snapshot.
- **يتصل بـ:** `failover_manager` يستعلم قبل كل محاولة.
- **متى يُستخدم:** كل operation لـ remote target يفحص breaker أولاً.

### `failover_manager.py`
- **الوظيفة:** orchestrator يدمج breaker + classifier + retry — `try_with_failover(targets, attempt)`.
- **يستقبل:** قائمة targets + attempt callable.
- **ينتج:** قيمة الناجح أو يرمي `FailoverError`.
- **يتصل بـ:** `circuit_breaker`, `failure_classifier`, `retry_queue`.
- **متى يُستخدم:** كل call إلى remote target يجب أن يلف بـ failover.

### `recovery_manager.py`
- **الوظيفة:** subscriber على bus + watchdog يشغّل recovery actions تلقائيًا.
- **يستقبل:** events من resilience_events bus.
- **ينتج:** تنظيف phi accrual، تشغيل state_sync، تسجيل audit، إلخ.
- **يتصل بـ:** `services/state_reconciliation`, `services/anti_entropy`, `services/audit_replication`, `services/phi_accrual`.
- **متى يُستخدم:** يعمل دائمًا في الخلفية حالما يبدأ الـ resilience manager.

### `degraded_mode.py`
- **الوظيفة:** يعيد حساب مستوى الـ cluster (NORMAL/DEGRADED/EMERGENCY) كل 10s.
- **يستقبل:** قراءات من partition_detector + backpressure + phi suspect rate.
- **ينتج:** `decide(essential=True/False) -> (allow, level)`.
- **يتصل بـ:** `services/partition_detector`, `services/backpressure`, `services/phi_accrual`.
- **متى يُستخدم:** قبل أي عملية قابلة للرفض في حالات overload.

### `resilience_manager.py`
- **الوظيفة:** الواجهة العليا — يبدأ/يوقف retry_queue + recovery + degraded loops.
- **يستقبل:** start/stop calls.
- **ينتج:** snapshot يجمع كل subsystems + recent events.
- **يتصل بـ:** كل ملفات resilience داخليًا، `app/main.py` خارجيًا.
- **متى يُستخدم:** عند startup/shutdown.

---

## 5. كيف يتعامل مع فشل العقد والمسارات والروابط

### مسار الفشل (failure path)

```
1. caller → failover_manager.try_with_failover([t1, t2, t3], attempt)
2. for each t in [t1, t2, t3]:
     a. circuit_breaker.allow(t)?  لا → skip
     b. await attempt(t)
     c. on success → breaker.record_success(t) + return
     d. on exception/5xx:
         - failure_classifier.classify_*  →  FailureKind
         - breaker.record_failure(t)
         - if not is_retryable(kind): raise FailoverError
         - if all targets fail:
             retry_queue.enqueue(...)  ← إعادة محاولة بـ exponential backoff
             raise FailoverError
3. recovery_manager (background):
     - يستمع لـ "breaker.open" → يجلي target من phi accrual
     - يستمع لـ "partition.detected" → يشغّل state_sync + anti_entropy
     - يستمع لـ "retry.exhausted" → يكتب في audit chain
4. degraded_mode (background):
     - كل 10s يفحص majority + backpressure + suspect_rate
     - يرفع الـ level إلى DEGRADED أو EMERGENCY عند الحاجة
     - callers يفحصون decide() قبل العمليات غير الجوهرية
```

### الربط مع باقي المشروع (read-only)

| Module | كيف يربطه resilience |
|---|---|
| **`routing_strategy`** | يستهلك `failure_classifier` لتصنيف ردود `send()`. لا import مباشر. |
| **`topology`** | recovery_manager يطلق `state_sync` بعد partition.detected — ينعكس على `topology.graph` تلقائيًا. |
| **`p2p`** | `failure_detector` يحكم على peer-aliveness عبر phi، `circuit_breaker` يطبّق per-peer policies. |
| **`overlay`** | overlay routes تستخدم failover_manager عند dispatch القفزات. |
| **`security`** | تصنيف SECURITY يمنع retry — يحول قرار للـ sync_policy. |
| **`storage`** | retry_queue persistent JSON-lines؛ recovery يكتب events للـ audit_replication. |
| **`monitoring`** | metrics_collector يقرأ `resilience_manager.snapshot()` كأحد المصادر. |

---

## التشغيل التلقائي

```python
# app/main.py lifespan
from app.resilience import start_resilience
start_resilience()
```

## API الإدارية

```
GET /api/admin/peers/resilience/snapshot
```

## نتائج التحقق

| اختبار | نتيجة |
|---|---|
| classify(500) | TRANSIENT ✓ |
| classify(429) | OVERLOAD ✓ |
| classify(403) | SECURITY ✓ |
| classify(404) | PERMANENT ✓ |
| classify(TimeoutError) | TRANSIENT ✓ |
| is_retryable(SECURITY) | False ✓ |
| compute_delay(0..3) | 1s..8s ✓ |
| circuit_breaker 6 fails → state | OPEN ✓ |
| breaker.allow(open) | False ✓ |
| degraded.tick() initial | NORMAL ✓ |
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
  app/resilience/           13 ملف  ← هذه الجولة

Total: 109 ملف موديولار + 24 خدمة في app/services/
859/859 pytest passed
Helen-Server.exe = 18 MB
كل ملف مسؤولية واحدة، إضافة بدون لمس البقية
```
