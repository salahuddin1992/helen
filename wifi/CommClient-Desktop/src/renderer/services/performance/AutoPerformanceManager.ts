/**
 * AutoPerformanceManager.ts — Unified performance orchestrator.
 *
 * The single entry point that wires together all Phase 9 compatibility
 * services into a coherent system. It:
 *
 *   1. Detects device capabilities (DeviceCapabilityDetector — existing)
 *   2. Selects a default performance mode (HardwareProfiles)
 *   3. Starts the ResourceGovernor to monitor budgets
 *   4. Starts the RenderOptimizer to apply visual optimizations
 *   5. Creates the MediaBudgetController for audio-priority allocation
 *   6. Starts the BackgroundThrottler for idle suppression
 *   7. Wires PerformanceGuard metrics into the ResourceGovernor
 *   8. Wires ResourceGovernor severity into MediaBudgetController + RenderOptimizer
 *   9. Allows runtime mode switching (Eco / Balanced / Performance)
 *   10. Emits high-level status events for UI consumption
 *
 * Initialization flow:
 *   const apm = new AutoPerformanceManager();
 *   await apm.initialize();   // detect hardware, select mode, start all subsystems
 *   apm.onStatus(status => updateUI(status));
 *
 * Mode switching:
 *   apm.setMode('eco');       // manual override
 *   apm.setAutoMode();        // return to auto-detection
 *
 * Lifecycle:
 *   apm.destroy();            // clean shutdown of all subsystems
 *
 * Does NOT modify any existing service. Composes them additively.
 */

import {
  detectDeviceCapabilities,
  getCachedProfile,
  type DeviceTier,
  type DeviceProfile,
} from './DeviceCapabilityDetector';
import {
  getProfile,
  getDefaultModeForTier,
  checkMinimumRequirements,
  shouldShowHardwareWarning,
  type PerformanceMode,
  type PerformanceProfile,
} from './HardwareProfiles';
import { ResourceGovernor, type GovernorAction, type GovernorSeverity, type ResourceMetrics } from './ResourceGovernor';
import { RenderOptimizer, type RenderMetrics } from './RenderOptimizer';
import { MediaBudgetController, type MediaAllocation } from './MediaBudgetController';
import { BackgroundThrottler, type AppVisibilityState, type ThrottlePolicy } from './BackgroundThrottler';
import { PerformanceGuard, type FrameMetrics } from './PerformanceGuard';

// ── Types ───────────────────────────────────────────────────

export interface PerformanceStatus {
  /** Whether the system has been initialized */
  initialized: boolean;
  /** Detected device tier */
  deviceTier: DeviceTier;
  /** Current active performance mode */
  activeMode: PerformanceMode;
  /** Whether the mode was auto-selected or user-overridden */
  isAutoMode: boolean;
  /** Current ResourceGovernor severity */
  severity: GovernorSeverity;
  /** Current app visibility state */
  visibility: AppVisibilityState;
  /** Hardware warnings (empty if meets minimum) */
  hardwareWarnings: string[];
  /** Current resource metrics */
  resources: ResourceMetrics | null;
  /** Current media allocation */
  media: MediaAllocation | null;
  /** Current render metrics */
  render: RenderMetrics | null;
  /** Timestamp */
  timestamp: number;
}

export interface PerformanceEvent {
  type:
    | 'initialized'
    | 'mode_changed'
    | 'severity_changed'
    | 'visibility_changed'
    | 'hardware_warning'
    | 'emergency';
  status: PerformanceStatus;
  detail?: string;
}

type StatusCallback = (status: PerformanceStatus) => void;
type EventCallback = (event: PerformanceEvent) => void;

// ── Constants ───────────────────────────────────────────────

const STORAGE_KEY_MODE = 'commclient_perf_mode';
const STORAGE_KEY_AUTO = 'commclient_perf_auto';
const STATUS_EMIT_DEBOUNCE_MS = 1_000;

// ── AutoPerformanceManager ──────────────────────────────────

export class AutoPerformanceManager {
  // Sub-systems
  private _governor: ResourceGovernor | null = null;
  private _renderOptimizer: RenderOptimizer | null = null;
  private _mediaBudget: MediaBudgetController | null = null;
  private _backgroundThrottler: BackgroundThrottler | null = null;
  private _performanceGuard: PerformanceGuard | null = null;

  // State
  private _initialized = false;
  private _deviceTier: DeviceTier = 'medium';
  private _deviceProfile: DeviceProfile | null = null;
  private _activeMode: PerformanceMode = 'balanced';
  private _activeProfile: PerformanceProfile | null = null;
  private _isAutoMode = true;
  private _hardwareWarnings: string[] = [];
  private _destroyed = false;

  // Listeners
  private _statusListeners: StatusCallback[] = [];
  private _eventListeners: EventCallback[] = [];
  private _statusEmitTimer: ReturnType<typeof setTimeout> | null = null;

  // Unsub functions from wired subsystems
  private _unsubscribers: Array<() => void> = [];

  // ── Initialization ────────────────────────────────────────

  /**
   * Initialize the entire performance subsystem.
   * Detects hardware, selects mode, starts all services.
   */
  async initialize(): Promise<PerformanceStatus> {
    if (this._initialized || this._destroyed) return this._buildStatus();

    // 1. Detect device capabilities
    this._deviceProfile = getCachedProfile() ?? await detectDeviceCapabilities();
    this._deviceTier = this._deviceProfile.tier;

    // 2. Check hardware warnings
    this._hardwareWarnings = checkMinimumRequirements({
      cpuCores: this._deviceProfile.cpuCores,
      memoryGB: this._deviceProfile.memoryGB,
      gpuRenderer: this._deviceProfile.gpuRenderer,
    });

    // 3. Select performance mode
    this._restoreSavedMode();
    this._activeProfile = getProfile(this._activeMode);

    // 4. Create and start subsystems
    this._createSubsystems();
    this._wireSubsystems();
    this._startSubsystems();

    this._initialized = true;

    const status = this._buildStatus();

    // Emit initialization event
    this._emitEvent({
      type: 'initialized',
      status,
      detail: `Device tier: ${this._deviceTier}, Mode: ${this._activeMode}`,
    });

    // Emit hardware warning if applicable
    if (this._hardwareWarnings.length > 0) {
      this._emitEvent({
        type: 'hardware_warning',
        status,
        detail: this._hardwareWarnings.join('; '),
      });
    }

    return status;
  }

  // ── Mode Management ───────────────────────────────────────

  /**
   * Set a specific performance mode (user override).
   */
  setMode(mode: PerformanceMode): void {
    this._isAutoMode = false;
    this._applyMode(mode);
    this._saveMode();
  }

  /**
   * Return to auto mode (let device tier decide).
   */
  setAutoMode(): void {
    this._isAutoMode = true;
    const autoMode = getDefaultModeForTier(this._deviceTier);
    this._applyMode(autoMode);
    this._saveMode();
  }

  /**
   * Get the current mode.
   */
  getMode(): PerformanceMode { return this._activeMode; }

  /**
   * Whether auto mode is active.
   */
  isAutoMode(): boolean { return this._isAutoMode; }

  // ── External State Feeds (from app to APM) ────────────────

  /**
   * Feed call state from call store.
   */
  feedCallState(isInCall: boolean, peerCount: number, isScreenSharing: boolean): void {
    this._governor?.feedPeerCount(peerCount);
    this._mediaBudget?.feedCallState(isInCall, peerCount, isScreenSharing);
    this._backgroundThrottler?.feedCallState(isInCall);
  }

  /**
   * Feed server connection state.
   */
  feedConnectionState(isConnected: boolean): void {
    this._backgroundThrottler?.feedConnectionState(isConnected);
  }

  // ── Getters for Sub-systems ───────────────────────────────

  getGovernor(): ResourceGovernor | null { return this._governor; }
  getRenderOptimizer(): RenderOptimizer | null { return this._renderOptimizer; }
  getMediaBudget(): MediaBudgetController | null { return this._mediaBudget; }
  getBackgroundThrottler(): BackgroundThrottler | null { return this._backgroundThrottler; }
  getPerformanceGuard(): PerformanceGuard | null { return this._performanceGuard; }

  // ── Event Subscription ────────────────────────────────────

  /**
   * Subscribe to periodic status updates (debounced).
   */
  onStatus(cb: StatusCallback): () => void {
    this._statusListeners.push(cb);
    return () => {
      this._statusListeners = this._statusListeners.filter(l => l !== cb);
    };
  }

  /**
   * Subscribe to discrete performance events.
   */
  onEvent(cb: EventCallback): () => void {
    this._eventListeners.push(cb);
    return () => {
      this._eventListeners = this._eventListeners.filter(l => l !== cb);
    };
  }

  // ── Get Current Status ────────────────────────────────────

  getStatus(): PerformanceStatus {
    return this._buildStatus();
  }

  /**
   * Get the media constraints for getUserMedia / getDisplayMedia.
   */
  getMediaConstraints(): ReturnType<MediaBudgetController['getMediaConstraints']> | null {
    return this._mediaBudget?.getMediaConstraints() ?? null;
  }

  /**
   * Check if a list should be virtualized.
   */
  shouldVirtualize(listSize: number): boolean {
    return this._renderOptimizer?.shouldVirtualize(listSize) ?? listSize > 100;
  }

  // ── Lifecycle ─────────────────────────────────────────────

  destroy(): void {
    this._destroyed = true;

    // Unsub all wired connections
    for (const unsub of this._unsubscribers) {
      try { unsub(); } catch {}
    }
    this._unsubscribers = [];

    // Destroy all subsystems
    this._performanceGuard?.destroy();
    this._governor?.destroy();
    this._renderOptimizer?.destroy();
    this._mediaBudget?.destroy();
    this._backgroundThrottler?.destroy();

    if (this._statusEmitTimer) {
      clearTimeout(this._statusEmitTimer);
      this._statusEmitTimer = null;
    }

    this._statusListeners = [];
    this._eventListeners = [];
  }

  // ── Internal: Subsystem Creation ──────────────────────────

  private _createSubsystems(): void {
    const p = this._activeProfile!;

    this._performanceGuard = new PerformanceGuard();
    this._governor = new ResourceGovernor(p.resource);
    this._renderOptimizer = new RenderOptimizer();
    this._mediaBudget = new MediaBudgetController(p.media);
    this._backgroundThrottler = new BackgroundThrottler(p.background);
  }

  private _wireSubsystems(): void {
    const guard = this._performanceGuard!;
    const gov = this._governor!;
    const render = this._renderOptimizer!;
    const media = this._mediaBudget!;
    const bg = this._backgroundThrottler!;

    // PerformanceGuard → ResourceGovernor (CPU pressure + FPS)
    this._unsubscribers.push(
      guard.onMetrics((metrics: FrameMetrics) => {
        gov.feedCpuPressure(metrics.cpuPressure);
        gov.feedFps(metrics.fps);
      })
    );

    // ResourceGovernor → MediaBudgetController (severity)
    this._unsubscribers.push(
      gov.on((action: GovernorAction) => {
        media.feedSeverity(action.severity);

        // Emit status on severity change
        this._scheduleStatusEmit();

        // Emit specific events
        if (action.type === 'warn_user' || action.type === 'emergency_gc') {
          this._emitEvent({
            type: action.severity >= 4 ? 'emergency' : 'severity_changed',
            status: this._buildStatus(),
            detail: action.reason,
          });
        }
      })
    );

    // BackgroundThrottler → status events
    this._unsubscribers.push(
      bg.onStateChange((state: AppVisibilityState) => {
        this._emitEvent({
          type: 'visibility_changed',
          status: this._buildStatus(),
          detail: `Visibility: ${state}`,
        });
      })
    );

    // RenderOptimizer → DOM complexity warnings
    this._unsubscribers.push(
      render.onComplexityWarning((warning) => {
        console.warn('[AutoPerformanceManager]', warning.message);
      })
    );
  }

  private _startSubsystems(): void {
    const p = this._activeProfile!;

    this._performanceGuard!.start();
    this._governor!.start();
    this._renderOptimizer!.start();
    this._renderOptimizer!.applyBudget(p.render);
    this._backgroundThrottler!.start();
  }

  // ── Internal: Mode Application ────────────────────────────

  private _applyMode(mode: PerformanceMode): void {
    if (mode === this._activeMode && this._activeProfile) return;

    this._activeMode = mode;
    this._activeProfile = getProfile(mode);

    // Update all subsystem budgets
    if (this._governor) {
      this._governor.updateBudget(this._activeProfile.resource);
    }
    if (this._renderOptimizer) {
      this._renderOptimizer.applyBudget(this._activeProfile.render);
    }
    if (this._mediaBudget) {
      this._mediaBudget.updateBudget(this._activeProfile.media);
    }
    if (this._backgroundThrottler) {
      this._backgroundThrottler.updateBudget(this._activeProfile.background);
    }

    // Emit mode change event
    this._emitEvent({
      type: 'mode_changed',
      status: this._buildStatus(),
      detail: `Mode switched to ${mode}`,
    });
  }

  // ── Internal: Persistence ─────────────────────────────────

  private _restoreSavedMode(): void {
    try {
      const savedAuto = localStorage.getItem(STORAGE_KEY_AUTO);
      const savedMode = localStorage.getItem(STORAGE_KEY_MODE) as PerformanceMode | null;

      if (savedAuto === 'false' && savedMode && ['eco', 'balanced', 'performance'].includes(savedMode)) {
        this._isAutoMode = false;
        this._activeMode = savedMode;
      } else {
        this._isAutoMode = true;
        this._activeMode = getDefaultModeForTier(this._deviceTier);
      }
    } catch {
      this._isAutoMode = true;
      this._activeMode = getDefaultModeForTier(this._deviceTier);
    }
  }

  private _saveMode(): void {
    try {
      localStorage.setItem(STORAGE_KEY_AUTO, String(this._isAutoMode));
      localStorage.setItem(STORAGE_KEY_MODE, this._activeMode);
    } catch {}
  }

  // ── Internal: Status Building ─────────────────────────────

  private _buildStatus(): PerformanceStatus {
    return {
      initialized: this._initialized,
      deviceTier: this._deviceTier,
      activeMode: this._activeMode,
      isAutoMode: this._isAutoMode,
      severity: this._governor?.getSeverity() ?? 0,
      visibility: this._backgroundThrottler?.getState() ?? 'active',
      hardwareWarnings: this._hardwareWarnings,
      resources: this._governor?.getMetrics() ?? null,
      media: this._mediaBudget?.getAllocation() ?? null,
      render: this._renderOptimizer?.getMetrics() ?? null,
      timestamp: Date.now(),
    };
  }

  // ── Internal: Event Emission ──────────────────────────────

  private _scheduleStatusEmit(): void {
    if (this._statusEmitTimer) return;
    this._statusEmitTimer = setTimeout(() => {
      this._statusEmitTimer = null;
      const status = this._buildStatus();
      for (const cb of this._statusListeners) {
        try { cb(status); } catch {}
      }
    }, STATUS_EMIT_DEBOUNCE_MS);
  }

  private _emitEvent(event: PerformanceEvent): void {
    for (const cb of this._eventListeners) {
      try { cb(event); } catch {}
    }
    // Also trigger status emit
    this._scheduleStatusEmit();
  }
}

// ── Singleton ───────────────────────────────────────────────

export const autoPerformanceManager = new AutoPerformanceManager();
