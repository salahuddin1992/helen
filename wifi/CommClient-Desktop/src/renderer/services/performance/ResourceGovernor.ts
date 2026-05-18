/**
 * ResourceGovernor.ts — CPU/RAM/GPU budget enforcement engine.
 *
 * Monitors actual resource consumption and enforces budget limits defined
 * by the active PerformanceProfile. When usage exceeds the budget, the
 * governor emits throttle/release signals that other subsystems react to.
 *
 * Monitoring sources:
 *   - CPU pressure: PerformanceGuard.getCpuPressure() (frame-based)
 *   - Heap memory: performance.memory (Chromium-only)
 *   - GPU proxy: canvas render time from DeviceCapabilityDetector
 *   - Peer count: active WebRTC connections
 *
 * Enforcement actions (signals, not direct mutations):
 *   - throttle_media   → MediaBudgetController reduces quality
 *   - throttle_render   → RenderOptimizer disables effects
 *   - throttle_background → BackgroundThrottler increases intervals
 *   - release           → subsystems restore normal operation
 *   - emergency_gc      → hint GC + drop non-critical caches
 *   - warn_user         → show low-resources notification
 *
 * Architecture:
 *   ResourceGovernor reads budgets from HardwareProfiles, polls metrics
 *   every POLL_INTERVAL_MS, computes a severity level (0-4), and emits
 *   GovernorAction events. It does NOT directly modify other services —
 *   AutoPerformanceManager wires signals to concrete actions.
 */

import type { ResourceBudget } from './HardwareProfiles';

// ── Types ───────────────────────────────────────────────────

export type GovernorSeverity = 0 | 1 | 2 | 3 | 4;
// 0 = nominal, 1 = elevated, 2 = throttle, 3 = heavy throttle, 4 = emergency

export type GovernorActionType =
  | 'nominal'
  | 'throttle_media'
  | 'throttle_render'
  | 'throttle_background'
  | 'release'
  | 'emergency_gc'
  | 'warn_user';

export interface GovernorAction {
  type: GovernorActionType;
  severity: GovernorSeverity;
  reason: string;
  metrics: ResourceMetrics;
  timestamp: number;
}

export interface ResourceMetrics {
  /** CPU pressure 0-1 from PerformanceGuard */
  cpuPressure: number;
  /** JS heap used (MB) */
  heapUsedMB: number;
  /** JS heap limit (MB) */
  heapLimitMB: number;
  /** Heap usage ratio 0-1 */
  heapRatio: number;
  /** Active peer connections */
  activePeerCount: number;
  /** Frames per second */
  fps: number;
  /** Current severity level */
  severity: GovernorSeverity;
}

type GovernorCallback = (action: GovernorAction) => void;

// ── Constants ───────────────────────────────────────────────

const POLL_INTERVAL_MS = 3_000;
const SEVERITY_HYSTERESIS_MS = 5_000;   // minimum time between severity changes
const HEAP_WARNING_RATIO = 0.75;        // 75% of heap limit
const HEAP_CRITICAL_RATIO = 0.90;       // 90% of heap limit
const CPU_ELEVATED_THRESHOLD = 0.40;
const CPU_THROTTLE_THRESHOLD = 0.60;
const CPU_HEAVY_THRESHOLD = 0.80;
const CPU_EMERGENCY_THRESHOLD = 0.95;
const FPS_LOW_THRESHOLD = 20;
const FPS_CRITICAL_THRESHOLD = 10;

// ── ResourceGovernor ────────────────────────────────────────

export class ResourceGovernor {
  private _budget: ResourceBudget;
  private _severity: GovernorSeverity = 0;
  private _lastSeverityChange = 0;
  private _pollTimer: ReturnType<typeof setInterval> | null = null;
  private _listeners: GovernorCallback[] = [];
  private _destroyed = false;

  // External metric feeds
  private _cpuPressure = 0;
  private _fps = 60;
  private _activePeerCount = 0;

  constructor(budget: ResourceBudget) {
    this._budget = budget;
  }

  // ── Lifecycle ─────────────────────────────────────────────

  start(): void {
    if (this._destroyed) return;
    this._lastSeverityChange = Date.now();
    this._pollTimer = setInterval(() => this._evaluate(), POLL_INTERVAL_MS);
    // First evaluation immediately
    this._evaluate();
  }

  stop(): void {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  destroy(): void {
    this._destroyed = true;
    this.stop();
    this._listeners = [];
  }

  // ── Configuration ─────────────────────────────────────────

  updateBudget(budget: ResourceBudget): void {
    this._budget = budget;
    // Re-evaluate with new budget immediately
    this._evaluate();
  }

  // ── External Metric Feeds ─────────────────────────────────

  /**
   * Feed CPU pressure from PerformanceGuard (0-1).
   */
  feedCpuPressure(pressure: number): void {
    this._cpuPressure = Math.max(0, Math.min(1, pressure));
  }

  /**
   * Feed current FPS from PerformanceGuard.
   */
  feedFps(fps: number): void {
    this._fps = fps;
  }

  /**
   * Feed active peer connection count from CallStore.
   */
  feedPeerCount(count: number): void {
    this._activePeerCount = count;
  }

  // ── Event Subscription ────────────────────────────────────

  on(cb: GovernorCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  // ── Get Current State ─────────────────────────────────────

  getSeverity(): GovernorSeverity { return this._severity; }

  getMetrics(): ResourceMetrics {
    return this._buildMetrics();
  }

  // ── Internal: Evaluation ──────────────────────────────────

  private _evaluate(): void {
    if (this._destroyed) return;

    const metrics = this._buildMetrics();
    const newSeverity = this._computeSeverity(metrics);

    // Apply hysteresis: don't change severity too frequently
    const now = Date.now();
    const timeSinceChange = now - this._lastSeverityChange;

    if (newSeverity !== this._severity) {
      // Allow immediate escalation (getting worse) but debounce de-escalation
      if (newSeverity > this._severity || timeSinceChange >= SEVERITY_HYSTERESIS_MS) {
        const previousSeverity = this._severity;
        this._severity = newSeverity;
        this._lastSeverityChange = now;

        // Emit appropriate actions
        this._emitActionsForTransition(previousSeverity, newSeverity, metrics);
      }
    }
  }

  private _buildMetrics(): ResourceMetrics {
    let heapUsedMB = 0;
    let heapLimitMB = 0;
    try {
      const mem = (performance as any).memory;
      if (mem) {
        heapUsedMB = Math.round(mem.usedJSHeapSize / 1024 / 1024);
        heapLimitMB = Math.round(mem.jsHeapSizeLimit / 1024 / 1024);
      }
    } catch {}

    const heapRatio = heapLimitMB > 0 ? heapUsedMB / heapLimitMB : 0;

    return {
      cpuPressure: this._cpuPressure,
      heapUsedMB,
      heapLimitMB,
      heapRatio,
      activePeerCount: this._activePeerCount,
      fps: this._fps,
      severity: this._severity,
    };
  }

  private _computeSeverity(metrics: ResourceMetrics): GovernorSeverity {
    const { cpuPressure, heapRatio, fps, activePeerCount } = metrics;

    // ── Emergency (4): system is in danger of crashing ──────
    if (
      cpuPressure >= CPU_EMERGENCY_THRESHOLD ||
      heapRatio >= HEAP_CRITICAL_RATIO ||
      fps < FPS_CRITICAL_THRESHOLD
    ) {
      return 4;
    }

    // ── Heavy throttle (3): significant resource pressure ───
    if (
      cpuPressure >= CPU_HEAVY_THRESHOLD ||
      heapRatio >= HEAP_WARNING_RATIO ||
      fps < FPS_LOW_THRESHOLD ||
      activePeerCount > this._budget.maxPeerConnections
    ) {
      return 3;
    }

    // ── Throttle (2): above budget, need to cut back ────────
    if (
      cpuPressure >= CPU_THROTTLE_THRESHOLD ||
      (metrics.heapUsedMB > this._budget.targetHeapMB)
    ) {
      return 2;
    }

    // ── Elevated (1): approaching budget limits ─────────────
    if (
      cpuPressure >= CPU_ELEVATED_THRESHOLD ||
      (metrics.heapUsedMB > this._budget.targetHeapMB * 0.8)
    ) {
      return 1;
    }

    // ── Nominal (0): within budget ──────────────────────────
    return 0;
  }

  private _emitActionsForTransition(
    from: GovernorSeverity,
    to: GovernorSeverity,
    metrics: ResourceMetrics,
  ): void {
    const now = Date.now();

    // De-escalation: release throttles
    if (to < from) {
      if (to === 0) {
        this._emit({ type: 'release', severity: to, reason: 'All metrics within budget', metrics, timestamp: now });
      } else {
        this._emit({ type: 'nominal', severity: to, reason: `Severity reduced from ${from} to ${to}`, metrics, timestamp: now });
      }
      return;
    }

    // Escalation
    const reasons: string[] = [];
    if (metrics.cpuPressure >= CPU_THROTTLE_THRESHOLD) {
      reasons.push(`CPU pressure ${(metrics.cpuPressure * 100).toFixed(0)}%`);
    }
    if (metrics.heapUsedMB > this._budget.targetHeapMB) {
      reasons.push(`Heap ${metrics.heapUsedMB}MB exceeds budget ${this._budget.targetHeapMB}MB`);
    }
    if (metrics.fps < FPS_LOW_THRESHOLD) {
      reasons.push(`FPS dropped to ${metrics.fps}`);
    }
    if (metrics.activePeerCount > this._budget.maxPeerConnections) {
      reasons.push(`${metrics.activePeerCount} peers exceeds limit ${this._budget.maxPeerConnections}`);
    }

    const reason = reasons.join('; ') || `Severity escalated to ${to}`;

    switch (to) {
      case 1:
        this._emit({ type: 'throttle_background', severity: to, reason, metrics, timestamp: now });
        break;
      case 2:
        this._emit({ type: 'throttle_render', severity: to, reason, metrics, timestamp: now });
        this._emit({ type: 'throttle_background', severity: to, reason, metrics, timestamp: now });
        break;
      case 3:
        this._emit({ type: 'throttle_media', severity: to, reason, metrics, timestamp: now });
        this._emit({ type: 'throttle_render', severity: to, reason, metrics, timestamp: now });
        this._emit({ type: 'throttle_background', severity: to, reason, metrics, timestamp: now });
        break;
      case 4:
        this._emit({ type: 'emergency_gc', severity: to, reason, metrics, timestamp: now });
        this._emit({ type: 'throttle_media', severity: to, reason, metrics, timestamp: now });
        this._emit({ type: 'throttle_render', severity: to, reason, metrics, timestamp: now });
        this._emit({ type: 'warn_user', severity: to, reason, metrics, timestamp: now });
        break;
    }
  }

  private _emit(action: GovernorAction): void {
    for (const cb of this._listeners) {
      try { cb(action); } catch {}
    }
  }
}
