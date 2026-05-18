/**
 * BackgroundThrottler.ts — Idle/background service suppression.
 *
 * Reduces resource usage when the application is:
 *   1. Minimized to tray (heaviest throttling)
 *   2. Not focused / behind other windows (moderate throttling)
 *   3. User idle (no input for N minutes) (light throttling)
 *
 * What gets throttled:
 *   - Socket.IO polling frequency (longer intervals)
 *   - Discovery broadcasts (suspended when connected)
 *   - Performance monitoring intervals (relaxed)
 *   - Network quality probes (less frequent)
 *   - Store subscription batching (longer windows)
 *   - Video tracks (disabled when hidden)
 *   - Non-critical timers (paused)
 *
 * What is NEVER throttled:
 *   - Incoming call notifications (always instant)
 *   - Incoming message delivery (always processed)
 *   - Audio playback (ringtones, notifications)
 *   - Active call audio/video (handled by MediaBudgetController)
 *
 * Architecture:
 *   BackgroundThrottler listens to document visibility, window focus,
 *   and user input events. It computes an AppVisibilityState and emits
 *   ThrottlePolicy objects that other services read.
 */

import type { BackgroundBudget } from './HardwareProfiles';

// ── Types ───────────────────────────────────────────────────

export type AppVisibilityState = 'active' | 'unfocused' | 'hidden' | 'idle';

export interface ThrottlePolicy {
  /** Current visibility state */
  state: AppVisibilityState;
  /** Multiplier for timer intervals (1 = normal, 2 = double, etc.) */
  timerMultiplier: number;
  /** Whether to disable outgoing video tracks */
  disableVideo: boolean;
  /** Whether to pause non-critical timers */
  pauseNonCritical: boolean;
  /** Whether to suspend discovery broadcasts */
  suspendDiscovery: boolean;
  /** Socket polling interval override (ms), 0 = use default */
  socketPollingMs: number;
  /** Whether to reduce store update frequency */
  batchStoreUpdates: boolean;
  /** Store update batching window (ms) */
  storeUpdateBatchMs: number;
  /** Timestamp of state transition */
  timestamp: number;
}

type ThrottleCallback = (policy: ThrottlePolicy) => void;
type StateCallback = (state: AppVisibilityState) => void;

// ── Constants ───────────────────────────────────────────────

const IDLE_TIMEOUT_MS = 5 * 60_000;           // 5 minutes without input → idle
const IDLE_CHECK_INTERVAL_MS = 30_000;         // Check idle every 30s
const INPUT_EVENTS = ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'wheel'];

// ── BackgroundThrottler ─────────────────────────────────────

export class BackgroundThrottler {
  private _budget: BackgroundBudget;
  private _state: AppVisibilityState = 'active';
  private _lastInputTime = Date.now();
  private _idleCheckTimer: ReturnType<typeof setInterval> | null = null;
  private _policyListeners: ThrottleCallback[] = [];
  private _stateListeners: StateCallback[] = [];
  private _destroyed = false;
  private _isInCall = false;
  private _isConnectedToServer = false;

  // Bound handlers for cleanup
  private _onVisibilityChange: () => void;
  private _onFocus: () => void;
  private _onBlur: () => void;
  private _onInput: () => void;

  constructor(budget: BackgroundBudget) {
    this._budget = budget;

    this._onVisibilityChange = this._handleVisibilityChange.bind(this);
    this._onFocus = this._handleFocus.bind(this);
    this._onBlur = this._handleBlur.bind(this);
    this._onInput = this._handleInput.bind(this);
  }

  // ── Lifecycle ─────────────────────────────────────────────

  start(): void {
    if (this._destroyed) return;

    // Visibility API
    document.addEventListener('visibilitychange', this._onVisibilityChange);

    // Window focus
    window.addEventListener('focus', this._onFocus);
    window.addEventListener('blur', this._onBlur);

    // User input (for idle detection)
    for (const event of INPUT_EVENTS) {
      document.addEventListener(event, this._onInput, { passive: true, capture: true });
    }

    // Idle check timer
    this._idleCheckTimer = setInterval(() => this._checkIdle(), IDLE_CHECK_INTERVAL_MS);

    // Initialize state
    this._evaluateState();
  }

  stop(): void {
    document.removeEventListener('visibilitychange', this._onVisibilityChange);
    window.removeEventListener('focus', this._onFocus);
    window.removeEventListener('blur', this._onBlur);

    for (const event of INPUT_EVENTS) {
      document.removeEventListener(event, this._onInput, { capture: true });
    }

    if (this._idleCheckTimer) {
      clearInterval(this._idleCheckTimer);
      this._idleCheckTimer = null;
    }
  }

  destroy(): void {
    this._destroyed = true;
    this.stop();
    this._policyListeners = [];
    this._stateListeners = [];
  }

  // ── Configuration ─────────────────────────────────────────

  updateBudget(budget: BackgroundBudget): void {
    this._budget = budget;
    this._emitPolicy();
  }

  // ── External State Feeds ──────────────────────────────────

  /**
   * Notify that the user is in an active call.
   * During calls, video throttling is handled by MediaBudgetController,
   * and we avoid pausing critical call-related timers.
   */
  feedCallState(isInCall: boolean): void {
    this._isInCall = isInCall;
    this._emitPolicy();
  }

  /**
   * Notify server connection state (for discovery suspension).
   */
  feedConnectionState(isConnected: boolean): void {
    this._isConnectedToServer = isConnected;
    this._emitPolicy();
  }

  // ── Event Subscription ────────────────────────────────────

  onPolicy(cb: ThrottleCallback): () => void {
    this._policyListeners.push(cb);
    return () => {
      this._policyListeners = this._policyListeners.filter(l => l !== cb);
    };
  }

  onStateChange(cb: StateCallback): () => void {
    this._stateListeners.push(cb);
    return () => {
      this._stateListeners = this._stateListeners.filter(l => l !== cb);
    };
  }

  // ── Get Current State ─────────────────────────────────────

  getState(): AppVisibilityState { return this._state; }

  getPolicy(): ThrottlePolicy {
    return this._buildPolicy();
  }

  /**
   * Time since last user input (ms).
   */
  getIdleDurationMs(): number {
    return Date.now() - this._lastInputTime;
  }

  // ── Event Handlers ────────────────────────────────────────

  private _handleVisibilityChange(): void {
    this._evaluateState();
  }

  private _handleFocus(): void {
    this._lastInputTime = Date.now();
    this._evaluateState();
  }

  private _handleBlur(): void {
    this._evaluateState();
  }

  private _handleInput(): void {
    const wasIdle = this._state === 'idle';
    this._lastInputTime = Date.now();

    if (wasIdle) {
      this._evaluateState();
    }
  }

  private _checkIdle(): void {
    if (this._destroyed) return;

    const idleMs = Date.now() - this._lastInputTime;
    if (idleMs >= IDLE_TIMEOUT_MS && this._state !== 'hidden' && this._state !== 'idle') {
      this._setState('idle');
    }
  }

  // ── State Evaluation ──────────────────────────────────────

  private _evaluateState(): void {
    if (this._destroyed) return;

    let newState: AppVisibilityState;

    if (document.hidden) {
      // Tab/window is completely hidden (minimized or behind)
      newState = 'hidden';
    } else if (!document.hasFocus()) {
      // Window is visible but not focused
      newState = 'unfocused';
    } else if (Date.now() - this._lastInputTime >= IDLE_TIMEOUT_MS) {
      // Focused but user hasn't interacted
      newState = 'idle';
    } else {
      newState = 'active';
    }

    this._setState(newState);
  }

  private _setState(newState: AppVisibilityState): void {
    if (newState === this._state) return;

    this._state = newState;

    // Notify state listeners
    for (const cb of this._stateListeners) {
      try { cb(newState); } catch {}
    }

    // Emit new policy
    this._emitPolicy();
  }

  // ── Policy Construction ───────────────────────────────────

  private _buildPolicy(): ThrottlePolicy {
    const b = this._budget;
    const state = this._state;

    switch (state) {
      case 'active':
        return {
          state,
          timerMultiplier: 1,
          disableVideo: false,
          pauseNonCritical: false,
          suspendDiscovery: b.suspendDiscoveryWhenConnected && this._isConnectedToServer,
          socketPollingMs: 0,  // default
          batchStoreUpdates: false,
          storeUpdateBatchMs: 0,
          timestamp: Date.now(),
        };

      case 'unfocused':
        return {
          state,
          timerMultiplier: 2,
          disableVideo: false,  // still visible, keep video
          pauseNonCritical: false,
          suspendDiscovery: b.suspendDiscoveryWhenConnected && this._isConnectedToServer,
          socketPollingMs: b.unfocusedThrottleMs,
          batchStoreUpdates: true,
          storeUpdateBatchMs: 100,
          timestamp: Date.now(),
        };

      case 'idle':
        return {
          state,
          timerMultiplier: 3,
          disableVideo: !this._isInCall,
          pauseNonCritical: b.pauseNonCriticalTimers,
          suspendDiscovery: b.suspendDiscoveryWhenConnected && this._isConnectedToServer,
          socketPollingMs: b.idleSocketIntervalMs,
          batchStoreUpdates: true,
          storeUpdateBatchMs: 200,
          timestamp: Date.now(),
        };

      case 'hidden':
        return {
          state,
          timerMultiplier: 5,
          disableVideo: b.disableVideoWhenHidden && !this._isInCall,
          pauseNonCritical: b.pauseNonCriticalTimers,
          suspendDiscovery: true,
          socketPollingMs: b.minimizedThrottleMs,
          batchStoreUpdates: true,
          storeUpdateBatchMs: 500,
          timestamp: Date.now(),
        };
    }
  }

  private _emitPolicy(): void {
    const policy = this._buildPolicy();
    for (const cb of this._policyListeners) {
      try { cb(policy); } catch {}
    }
  }
}
