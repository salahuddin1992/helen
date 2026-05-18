/**
 * GracefulDegradationEngine — Orchestrates the fallback hierarchy when
 * conditions degrade (weak device, bad WiFi, CPU overload).
 *
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║                    FALLBACK HIERARCHY                           ║
 * ║                                                                  ║
 * ║  Level 0: FULL         1080p60 + all features                   ║
 * ║  Level 1: REDUCED      720p30, cap group tile count             ║
 * ║  Level 2: CONSERVATIVE 480p24, disable animations, lazy load    ║
 * ║  Level 3: MINIMAL      360p15, audio priority, disable video bg ║
 * ║  Level 4: AUDIO_ONLY   Kill all video tracks, audio-only mode   ║
 * ║  Level 5: SURVIVAL     Reduce audio bitrate, batch messages     ║
 * ║                                                                  ║
 * ║  Downgrade: fast (2s debounce)                                  ║
 * ║  Upgrade:   slow (15s stability required)                       ║
 * ║                                                                  ║
 * ║  Audio is ALWAYS the last thing to degrade.                     ║
 * ╚══════════════════════════════════════════════════════════════════╝
 *
 * The engine combines signals from:
 *   - DeviceCapabilityDetector (hardware tier → initial ceiling)
 *   - NetworkQualityMonitor (network conditions → dynamic floor)
 *   - QualityController (per-peer WebRTC stats)
 *   - PerformanceGuard (UI frame budget → CPU pressure signal)
 *
 * It emits DegradationAction commands that the CallEngine, UI, and
 * messaging systems react to.
 */

import { DeviceTier, detectDeviceCapabilities, type DeviceProfile } from './DeviceCapabilityDetector';
import { NetworkQualityMonitor, type NetworkQuality, type NetworkSnapshot } from './NetworkQualityMonitor';

// ── Types ──────────────────────────────────────────────

export type DegradationLevel = 0 | 1 | 2 | 3 | 4 | 5;

export interface DegradationState {
  level: DegradationLevel;
  label: string;
  reason: DegradationReason;
  constraints: MediaConstraints;
  uiHints: UIHints;
  timestamp: number;
}

export type DegradationReason =
  | 'none'
  | 'device_weak'
  | 'network_poor'
  | 'network_critical'
  | 'cpu_overload'
  | 'group_size'
  | 'user_request'
  | 'combined';

export interface MediaConstraints {
  maxVideoWidth: number;
  maxVideoHeight: number;
  maxFramerate: number;
  maxVideoBitrateKbps: number;
  maxAudioBitrateKbps: number;
  videoEnabled: boolean;
  screenShareMaxFps: number;
  screenShareMaxWidth: number;
  screenShareMaxHeight: number;
}

export interface UIHints {
  disableAnimations: boolean;
  reduceParticleEffects: boolean;
  lazyLoadImages: boolean;
  throttleTypingIndicators: boolean;
  batchMessageUpdates: boolean;
  hideVideoThumbnails: boolean;
  maxVisibleVideoTiles: number;
  showDegradationBanner: boolean;
  bannerMessageKey: string;       // i18n key
}

export interface DegradationAction {
  type: 'degrade' | 'upgrade' | 'force_audio_only' | 'restore_video';
  fromLevel: DegradationLevel;
  toLevel: DegradationLevel;
  state: DegradationState;
}

type DegradationCallback = (action: DegradationAction) => void;

// ── Level Definitions ──────────────────────────────────

interface LevelDef {
  label: string;
  constraints: MediaConstraints;
  uiHints: UIHints;
}

const LEVELS: Record<DegradationLevel, LevelDef> = {
  0: {
    label: 'Full Quality',
    constraints: {
      maxVideoWidth: 1920, maxVideoHeight: 1080, maxFramerate: 60,
      maxVideoBitrateKbps: 10_000, maxAudioBitrateKbps: 128,
      videoEnabled: true,
      screenShareMaxFps: 30, screenShareMaxWidth: 1920, screenShareMaxHeight: 1080,
    },
    uiHints: {
      disableAnimations: false, reduceParticleEffects: false, lazyLoadImages: false,
      throttleTypingIndicators: false, batchMessageUpdates: false, hideVideoThumbnails: false,
      maxVisibleVideoTiles: 8, showDegradationBanner: false, bannerMessageKey: '',
    },
  },
  1: {
    label: 'Reduced',
    constraints: {
      maxVideoWidth: 1280, maxVideoHeight: 720, maxFramerate: 30,
      maxVideoBitrateKbps: 5_000, maxAudioBitrateKbps: 128,
      videoEnabled: true,
      screenShareMaxFps: 15, screenShareMaxWidth: 1920, screenShareMaxHeight: 1080,
    },
    uiHints: {
      disableAnimations: false, reduceParticleEffects: false, lazyLoadImages: false,
      throttleTypingIndicators: false, batchMessageUpdates: false, hideVideoThumbnails: false,
      maxVisibleVideoTiles: 6, showDegradationBanner: false, bannerMessageKey: '',
    },
  },
  2: {
    label: 'Conservative',
    constraints: {
      maxVideoWidth: 854, maxVideoHeight: 480, maxFramerate: 24,
      maxVideoBitrateKbps: 2_000, maxAudioBitrateKbps: 96,
      videoEnabled: true,
      screenShareMaxFps: 10, screenShareMaxWidth: 1280, screenShareMaxHeight: 720,
    },
    uiHints: {
      disableAnimations: true, reduceParticleEffects: true, lazyLoadImages: true,
      throttleTypingIndicators: true, batchMessageUpdates: false, hideVideoThumbnails: false,
      maxVisibleVideoTiles: 4, showDegradationBanner: true,
      bannerMessageKey: 'perf.degraded_quality',
    },
  },
  3: {
    label: 'Minimal Video',
    constraints: {
      maxVideoWidth: 640, maxVideoHeight: 360, maxFramerate: 15,
      maxVideoBitrateKbps: 500, maxAudioBitrateKbps: 64,
      videoEnabled: true,
      screenShareMaxFps: 5, screenShareMaxWidth: 854, screenShareMaxHeight: 480,
    },
    uiHints: {
      disableAnimations: true, reduceParticleEffects: true, lazyLoadImages: true,
      throttleTypingIndicators: true, batchMessageUpdates: true, hideVideoThumbnails: true,
      maxVisibleVideoTiles: 2, showDegradationBanner: true,
      bannerMessageKey: 'perf.low_quality',
    },
  },
  4: {
    label: 'Audio Only',
    constraints: {
      maxVideoWidth: 0, maxVideoHeight: 0, maxFramerate: 0,
      maxVideoBitrateKbps: 0, maxAudioBitrateKbps: 64,
      videoEnabled: false,
      screenShareMaxFps: 3, screenShareMaxWidth: 640, screenShareMaxHeight: 360,
    },
    uiHints: {
      disableAnimations: true, reduceParticleEffects: true, lazyLoadImages: true,
      throttleTypingIndicators: true, batchMessageUpdates: true, hideVideoThumbnails: true,
      maxVisibleVideoTiles: 0, showDegradationBanner: true,
      bannerMessageKey: 'perf.audio_only',
    },
  },
  5: {
    label: 'Survival',
    constraints: {
      maxVideoWidth: 0, maxVideoHeight: 0, maxFramerate: 0,
      maxVideoBitrateKbps: 0, maxAudioBitrateKbps: 32,
      videoEnabled: false,
      screenShareMaxFps: 0, screenShareMaxWidth: 0, screenShareMaxHeight: 0,
    },
    uiHints: {
      disableAnimations: true, reduceParticleEffects: true, lazyLoadImages: true,
      throttleTypingIndicators: true, batchMessageUpdates: true, hideVideoThumbnails: true,
      maxVisibleVideoTiles: 0, showDegradationBanner: true,
      bannerMessageKey: 'perf.survival_mode',
    },
  },
};

// ── Timing Constants ───────────────────────────────────

const DOWNGRADE_DEBOUNCE_MS = 2_000;
const UPGRADE_STABILITY_MS = 15_000;
const EVALUATION_INTERVAL_MS = 3_000;

// ── Engine Implementation ──────────────────────────────

export class GracefulDegradationEngine {
  private _currentLevel: DegradationLevel = 0;
  private _currentReason: DegradationReason = 'none';
  private _maxLevel: DegradationLevel = 0; // Hardware ceiling
  private _userForceLevel: DegradationLevel | null = null;

  private _lastDowngradeTime = 0;
  private _lastUpgradeTime = 0;
  private _stableGoodSince = 0; // timestamp when conditions became good enough to upgrade

  private _listeners: DegradationCallback[] = [];
  private _evalTimer: ReturnType<typeof setInterval> | null = null;
  private _destroyed = false;

  // External signal feeds
  private _networkQuality: NetworkQuality = 'excellent';
  private _cpuPressure: number = 0;  // 0-1, from PerformanceGuard
  private _groupSize: number = 1;
  private _deviceProfile: DeviceProfile | null = null;

  // ── Lifecycle ─────────────────────────────────────────

  /**
   * Initialize the engine. Runs device detection and sets the hardware ceiling.
   */
  init(): void {
    this._deviceProfile = detectDeviceCapabilities();
    this._maxLevel = this._tierToMaxLevel(this._deviceProfile.tier);

    // If device is weak, start at a degraded level
    if (this._deviceProfile.tier === 'minimal') {
      this._transitionTo(4, 'device_weak');
    } else if (this._deviceProfile.tier === 'low') {
      this._transitionTo(2, 'device_weak');
    } else if (this._deviceProfile.tier === 'medium') {
      this._transitionTo(1, 'device_weak');
    }

    this._evalTimer = setInterval(() => this._evaluate(), EVALUATION_INTERVAL_MS);
  }

  destroy(): void {
    this._destroyed = true;
    if (this._evalTimer) {
      clearInterval(this._evalTimer);
      this._evalTimer = null;
    }
    this._listeners = [];
  }

  // ── Event Subscription ────────────────────────────────

  on(cb: DegradationCallback): () => void {
    this._listeners.push(cb);
    return () => { this._listeners = this._listeners.filter(l => l !== cb); };
  }

  // ── External Signal Feeds ─────────────────────────────

  feedNetworkQuality(quality: NetworkQuality): void {
    this._networkQuality = quality;
  }

  feedCpuPressure(pressure: number): void {
    this._cpuPressure = Math.max(0, Math.min(1, pressure));
  }

  feedGroupSize(size: number): void {
    this._groupSize = Math.max(1, size);
  }

  // ── User Override ─────────────────────────────────────

  forceLevel(level: DegradationLevel): void {
    this._userForceLevel = level;
    this._transitionTo(level, 'user_request');
  }

  clearForceLevel(): void {
    this._userForceLevel = null;
  }

  forceAudioOnly(): void {
    this.forceLevel(4);
  }

  restoreVideo(): void {
    this.clearForceLevel();
    this._evaluate();
  }

  // ── Get State ─────────────────────────────────────────

  getState(): DegradationState {
    const levelDef = LEVELS[this._currentLevel];
    return {
      level: this._currentLevel,
      label: levelDef.label,
      reason: this._currentReason,
      constraints: { ...levelDef.constraints },
      uiHints: { ...levelDef.uiHints },
      timestamp: Date.now(),
    };
  }

  getLevel(): DegradationLevel { return this._currentLevel; }
  getConstraints(): MediaConstraints { return { ...LEVELS[this._currentLevel].constraints }; }
  getUIHints(): UIHints { return { ...LEVELS[this._currentLevel].uiHints }; }

  // ── Core Evaluation Loop ──────────────────────────────

  private _evaluate(): void {
    if (this._destroyed) return;
    if (this._userForceLevel !== null) return; // User override active

    const targetLevel = this._computeTargetLevel();

    if (targetLevel > this._currentLevel) {
      // DOWNGRADE — react quickly
      const now = Date.now();
      if (now - this._lastDowngradeTime >= DOWNGRADE_DEBOUNCE_MS) {
        this._stableGoodSince = 0;
        this._transitionTo(targetLevel, this._computeReason());
      }
    } else if (targetLevel < this._currentLevel) {
      // UPGRADE — require sustained good conditions
      const now = Date.now();
      if (this._stableGoodSince === 0) {
        this._stableGoodSince = now;
      } else if (now - this._stableGoodSince >= UPGRADE_STABILITY_MS) {
        // Only upgrade one level at a time
        const newLevel = (this._currentLevel - 1) as DegradationLevel;
        this._transitionTo(newLevel, this._computeReason());
        this._stableGoodSince = now; // Reset for next upgrade
      }
    } else {
      // Stable — reset upgrade timer if conditions aren't good enough
      this._stableGoodSince = 0;
    }
  }

  private _computeTargetLevel(): DegradationLevel {
    let level: DegradationLevel = 0;

    // Network quality signal
    switch (this._networkQuality) {
      case 'critical': level = Math.max(level, 5) as DegradationLevel; break;
      case 'poor':     level = Math.max(level, 4) as DegradationLevel; break;
      case 'fair':     level = Math.max(level, 2) as DegradationLevel; break;
      case 'good':     level = Math.max(level, 1) as DegradationLevel; break;
      case 'excellent': break;
    }

    // CPU pressure signal
    if (this._cpuPressure > 0.9) level = Math.max(level, 4) as DegradationLevel;
    else if (this._cpuPressure > 0.7) level = Math.max(level, 3) as DegradationLevel;
    else if (this._cpuPressure > 0.5) level = Math.max(level, 2) as DegradationLevel;

    // Group size pressure
    if (this._groupSize >= 7) level = Math.max(level, 2) as DegradationLevel;
    else if (this._groupSize >= 5) level = Math.max(level, 1) as DegradationLevel;

    // Hardware ceiling
    const hwFloor = this._tierToMinLevel(this._deviceProfile?.tier || 'medium');
    level = Math.max(level, hwFloor) as DegradationLevel;

    // Never exceed level 5
    return Math.min(level, 5) as DegradationLevel;
  }

  private _computeReason(): DegradationReason {
    const signals: DegradationReason[] = [];
    if (this._networkQuality === 'poor' || this._networkQuality === 'critical') signals.push('network_poor');
    if (this._cpuPressure > 0.5) signals.push('cpu_overload');
    if (this._deviceProfile && (this._deviceProfile.tier === 'low' || this._deviceProfile.tier === 'minimal')) signals.push('device_weak');
    if (this._groupSize >= 5) signals.push('group_size');

    if (signals.length === 0) return 'none';
    if (signals.length === 1) return signals[0];
    return 'combined';
  }

  private _transitionTo(level: DegradationLevel, reason: DegradationReason): void {
    if (level === this._currentLevel) return;

    const fromLevel = this._currentLevel;
    const isDowngrade = level > fromLevel;

    this._currentLevel = level;
    this._currentReason = reason;

    if (isDowngrade) {
      this._lastDowngradeTime = Date.now();
    } else {
      this._lastUpgradeTime = Date.now();
    }

    const state = this.getState();

    console.log(`[Degradation] ${isDowngrade ? '↓ DOWNGRADE' : '↑ UPGRADE'} Level ${fromLevel} → ${level} (${state.label}) reason=${reason}`);

    const action: DegradationAction = {
      type: level === 4 || level === 5 ? 'force_audio_only' :
            fromLevel >= 4 && level < 4 ? 'restore_video' :
            isDowngrade ? 'degrade' : 'upgrade',
      fromLevel,
      toLevel: level,
      state,
    };

    for (const cb of this._listeners) {
      try { cb(action); } catch {}
    }
  }

  private _tierToMaxLevel(tier: DeviceTier): DegradationLevel {
    switch (tier) {
      case 'high': return 0;
      case 'medium': return 0;
      case 'low': return 1;
      case 'minimal': return 2;
    }
  }

  private _tierToMinLevel(tier: DeviceTier): DegradationLevel {
    switch (tier) {
      case 'high': return 0;
      case 'medium': return 0;
      case 'low': return 1;
      case 'minimal': return 3;
    }
  }
}
