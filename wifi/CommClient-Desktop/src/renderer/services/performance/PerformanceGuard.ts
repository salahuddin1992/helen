/**
 * PerformanceGuard — UI performance watchdog.
 *
 * Monitors:
 *   - Frame budget (16.67ms for 60fps) via requestAnimationFrame timing
 *   - Long tasks (>50ms) via PerformanceObserver
 *   - JS heap pressure (via performance.memory)
 *   - Frozen frames (gaps >100ms between RAF callbacks)
 *
 * Outputs:
 *   - CPU pressure signal (0-1) fed into GracefulDegradationEngine
 *   - Jank detection events for logging
 *   - Render budget percentage consumed
 *
 * Also provides utilities:
 *   - throttledRAF: Request animation frame with auto-skip when janky
 *   - batchUpdates: Coalesce state updates to reduce re-renders
 *   - idleCallback: Schedule low-priority work during idle time
 */

// ── Types ──────────────────────────────────────────────

export interface FrameMetrics {
  fps: number;
  avgFrameTime: number;    // ms
  maxFrameTime: number;    // ms
  jankCount: number;       // frames > 33ms in last second
  frozenCount: number;     // frames > 100ms in last second
  cpuPressure: number;     // 0-1 composite
  heapUsedMB: number;
  heapLimitMB: number;
  longTaskCount: number;   // tasks > 50ms in last second
  timestamp: number;
}

export interface JankEvent {
  frameTime: number;       // ms
  timestamp: number;
  type: 'jank' | 'frozen';
}

type MetricsCallback = (metrics: FrameMetrics) => void;
type JankCallback = (event: JankEvent) => void;

// ── Constants ──────────────────────────────────────────

const TARGET_FPS = 60;
const FRAME_BUDGET_MS = 1000 / TARGET_FPS;      // 16.67ms
const JANK_THRESHOLD_MS = 33;                     // >2x budget
const FROZEN_THRESHOLD_MS = 100;                  // clearly frozen
const METRICS_EMIT_INTERVAL_MS = 2_000;           // emit metrics every 2s
const FRAME_HISTORY_SIZE = 120;                   // ~2s of frames at 60fps

// ── PerformanceGuard ───────────────────────────────────

export class PerformanceGuard {
  private _rafId: number | null = null;
  private _lastFrameTime = 0;
  private _frameTimings: number[] = [];
  private _jankCount = 0;
  private _frozenCount = 0;
  private _longTaskCount = 0;

  private _metricsListeners: MetricsCallback[] = [];
  private _jankListeners: JankCallback[] = [];
  private _metricsTimer: ReturnType<typeof setInterval> | null = null;
  private _longTaskObserver: PerformanceObserver | null = null;
  private _destroyed = false;

  // ── Lifecycle ─────────────────────────────────────────

  start(): void {
    if (this._destroyed) return;

    // Start RAF loop
    this._lastFrameTime = performance.now();
    this._tick(this._lastFrameTime);

    // Start long task observer
    try {
      this._longTaskObserver = new PerformanceObserver((list) => {
        this._longTaskCount += list.getEntries().length;
      });
      this._longTaskObserver.observe({ entryTypes: ['longtask'] });
    } catch {
      // PerformanceObserver longtask not supported in all environments
    }

    // Emit metrics periodically
    this._metricsTimer = setInterval(() => this._emitMetrics(), METRICS_EMIT_INTERVAL_MS);
  }

  stop(): void {
    this._destroyed = true;
    if (this._rafId !== null) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    if (this._metricsTimer) {
      clearInterval(this._metricsTimer);
      this._metricsTimer = null;
    }
    if (this._longTaskObserver) {
      this._longTaskObserver.disconnect();
      this._longTaskObserver = null;
    }
  }

  destroy(): void {
    this.stop();
    this._metricsListeners = [];
    this._jankListeners = [];
  }

  // ── Event Subscription ────────────────────────────────

  onMetrics(cb: MetricsCallback): () => void {
    this._metricsListeners.push(cb);
    return () => { this._metricsListeners = this._metricsListeners.filter(l => l !== cb); };
  }

  onJank(cb: JankCallback): () => void {
    this._jankListeners.push(cb);
    return () => { this._jankListeners = this._jankListeners.filter(l => l !== cb); };
  }

  // ── Get Current Pressure ──────────────────────────────

  getCpuPressure(): number {
    return this._computeCpuPressure();
  }

  getLatestMetrics(): FrameMetrics {
    return this._buildMetrics();
  }

  // ── RAF Loop ──────────────────────────────────────────

  private _tick = (now: number): void => {
    if (this._destroyed) return;

    const delta = now - this._lastFrameTime;
    this._lastFrameTime = now;

    // Record frame timing
    this._frameTimings.push(delta);
    if (this._frameTimings.length > FRAME_HISTORY_SIZE) {
      this._frameTimings.shift();
    }

    // Detect jank
    if (delta > FROZEN_THRESHOLD_MS) {
      this._frozenCount++;
      this._emitJank({ frameTime: delta, timestamp: now, type: 'frozen' });
    } else if (delta > JANK_THRESHOLD_MS) {
      this._jankCount++;
      this._emitJank({ frameTime: delta, timestamp: now, type: 'jank' });
    }

    this._rafId = requestAnimationFrame(this._tick);
  };

  // ── Metrics Computation ───────────────────────────────

  private _buildMetrics(): FrameMetrics {
    const timings = this._frameTimings;
    const count = timings.length;

    let avgFrameTime = FRAME_BUDGET_MS;
    let maxFrameTime = 0;
    if (count > 0) {
      avgFrameTime = timings.reduce((a, b) => a + b, 0) / count;
      maxFrameTime = Math.max(...timings);
    }

    const fps = avgFrameTime > 0 ? Math.min(TARGET_FPS, Math.round(1000 / avgFrameTime)) : 0;

    // Memory
    let heapUsedMB = 0;
    let heapLimitMB = 0;
    try {
      const mem = (performance as any).memory;
      if (mem) {
        heapUsedMB = Math.round(mem.usedJSHeapSize / 1024 / 1024);
        heapLimitMB = Math.round(mem.jsHeapSizeLimit / 1024 / 1024);
      }
    } catch {}

    return {
      fps,
      avgFrameTime: Math.round(avgFrameTime * 10) / 10,
      maxFrameTime: Math.round(maxFrameTime * 10) / 10,
      jankCount: this._jankCount,
      frozenCount: this._frozenCount,
      cpuPressure: this._computeCpuPressure(),
      heapUsedMB,
      heapLimitMB,
      longTaskCount: this._longTaskCount,
      timestamp: Date.now(),
    };
  }

  private _computeCpuPressure(): number {
    const timings = this._frameTimings;
    if (timings.length < 10) return 0;

    // Compute what fraction of frame budget is consumed
    const avg = timings.reduce((a, b) => a + b, 0) / timings.length;
    const budgetUsage = avg / FRAME_BUDGET_MS; // >1 means exceeding budget

    // Jank ratio
    const jankRatio = timings.filter(t => t > JANK_THRESHOLD_MS).length / timings.length;

    // Frozen ratio (severe)
    const frozenRatio = timings.filter(t => t > FROZEN_THRESHOLD_MS).length / timings.length;

    // Composite: 50% budget usage, 30% jank ratio, 20% frozen ratio
    let pressure = (
      Math.min(1, budgetUsage / 2) * 0.5 +
      jankRatio * 0.3 +
      frozenRatio * 0.2
    );

    // Long task penalty
    if (this._longTaskCount > 5) pressure = Math.min(1, pressure + 0.1);

    return Math.max(0, Math.min(1, pressure));
  }

  private _emitMetrics(): void {
    if (this._destroyed) return;
    const metrics = this._buildMetrics();

    // Reset counters
    this._jankCount = 0;
    this._frozenCount = 0;
    this._longTaskCount = 0;

    for (const cb of this._metricsListeners) {
      try { cb(metrics); } catch {}
    }
  }

  private _emitJank(event: JankEvent): void {
    for (const cb of this._jankListeners) {
      try { cb(event); } catch {}
    }
  }
}

// ── Utility: Throttled RAF ─────────────────────────────

/**
 * requestAnimationFrame wrapper that auto-skips frames when
 * the browser is janking. Runs at most once per `minInterval` ms.
 */
export function throttledRAF(callback: () => void, minInterval: number = 32): () => void {
  let lastRun = 0;
  let rafId: number | null = null;
  let cancelled = false;

  const tick = () => {
    if (cancelled) return;
    const now = performance.now();
    if (now - lastRun >= minInterval) {
      lastRun = now;
      callback();
    }
    rafId = requestAnimationFrame(tick);
  };

  rafId = requestAnimationFrame(tick);

  return () => {
    cancelled = true;
    if (rafId !== null) cancelAnimationFrame(rafId);
  };
}

// ── Utility: Batch Updates ─────────────────────────────

/**
 * Batches multiple calls to `fn` within a single microtask/RAF.
 * Useful for coalescing store updates that trigger re-renders.
 */
export function batchUpdates<T>(fn: (items: T[]) => void, delayMs: number = 0): (item: T) => void {
  let buffer: T[] = [];
  let scheduled = false;

  return (item: T) => {
    buffer.push(item);
    if (!scheduled) {
      scheduled = true;
      if (delayMs === 0) {
        queueMicrotask(() => {
          const items = buffer;
          buffer = [];
          scheduled = false;
          fn(items);
        });
      } else {
        setTimeout(() => {
          const items = buffer;
          buffer = [];
          scheduled = false;
          fn(items);
        }, delayMs);
      }
    }
  };
}

// ── Utility: Idle Callback ─────────────────────────────

/**
 * Schedule work during browser idle time. Falls back to setTimeout
 * if requestIdleCallback is not available.
 */
export function idleCallback(fn: () => void, timeout: number = 5000): void {
  if ('requestIdleCallback' in window) {
    (window as any).requestIdleCallback(fn, { timeout });
  } else {
    setTimeout(fn, 100);
  }
}
