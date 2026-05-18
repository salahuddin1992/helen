/**
 * QualityController — adaptive bitrate, resolution, and framerate controller.
 *
 * Periodically samples RTCStatsReport from all peer connections and adjusts
 * encoding parameters to maintain call quality. On LAN this is mostly a
 * safety net — bandwidth is rarely constrained — but it handles:
 *
 *   - CPU-bound degradation (encoder can't keep up → lower resolution/fps)
 *   - Transient packet loss (switch reconfiguration, Wi-Fi interference)
 *   - Multi-peer scaling (reduce per-peer bitrate as participants increase)
 *   - Quality presets (low / medium / high / lan-max)
 *
 * The controller operates in a feedback loop:
 *   1. Collect stats from all PeerConnections
 *   2. Compute quality score per peer
 *   3. Determine worst-case score across peers
 *   4. Adjust encoding parameters if score crosses thresholds
 *   5. Emit quality change events for UI feedback
 */

import { PeerConnection } from './PeerConnection';
import { GroupCallManager } from './GroupCallManager';

// ── Quality Presets ─────────────────────────────────

export interface QualityPreset {
  label: string;
  maxBitrateKbps: number;
  maxFramerate: number;
  idealWidth: number;
  idealHeight: number;
  /**
   * Opus audio bitrate cap (kbps). Optional so legacy callers/tests
   * that synthesise presets stay valid; QualityController falls back
   * to DEFAULT_AUDIO_BITRATE_KBPS when undefined.
   */
  audioBitrateKbps?: number;
}

export const QUALITY_PRESETS: Record<string, QualityPreset> = {
  // 8K — gated behind server-side allow_8k. Present in the map so the
  // admin can flip the flag without redeploying the client.
  '8k': {
    label: '8K (4320p)',
    maxBitrateKbps: 60_000,
    maxFramerate: 30,
    idealWidth: 7680,
    idealHeight: 4320,
    audioBitrateKbps: 128,
  },
  '4k-60': {
    label: '4K 60fps',
    maxBitrateKbps: 40_000,
    maxFramerate: 60,
    idealWidth: 3840,
    idealHeight: 2160,
    audioBitrateKbps: 128,
  },
  '4k': {
    label: '4K (2160p)',
    maxBitrateKbps: 25_000,
    maxFramerate: 30,
    idealWidth: 3840,
    idealHeight: 2160,
    audioBitrateKbps: 128,
  },
  '1440p': {
    label: '1440p QHD',
    maxBitrateKbps: 12_000,
    maxFramerate: 30,
    idealWidth: 2560,
    idealHeight: 1440,
    audioBitrateKbps: 96,
  },
  'lan-max': {
    label: 'LAN Maximum',
    maxBitrateKbps: 10_000,
    maxFramerate: 60,
    idealWidth: 1920,
    idealHeight: 1080,
    audioBitrateKbps: 96,
  },
  high: {
    label: 'High',
    maxBitrateKbps: 5_000,
    maxFramerate: 30,
    idealWidth: 1280,
    idealHeight: 720,
    audioBitrateKbps: 64,
  },
  medium: {
    label: 'Medium',
    maxBitrateKbps: 2_000,
    maxFramerate: 24,
    idealWidth: 854,
    idealHeight: 480,
    audioBitrateKbps: 48,
  },
  low: {
    label: 'Low',
    maxBitrateKbps: 500,
    maxFramerate: 15,
    idealWidth: 640,
    idealHeight: 360,
    audioBitrateKbps: 32,
  },
  'audio-only': {
    label: 'Audio Only',
    maxBitrateKbps: 0,
    maxFramerate: 0,
    idealWidth: 0,
    idealHeight: 0,
    audioBitrateKbps: 64,
  },
};

/** Audio bitrate used when a preset doesn't specify one. */
const DEFAULT_AUDIO_BITRATE_KBPS = 64;

// Presets ranked high → low. Used by the UI + auto-downgrade logic.
export const PRESET_ORDER: string[] = [
  '8k', '4k-60', '4k', '1440p', 'lan-max', 'high', 'medium', 'low', 'audio-only',
];

// ── Quality Score ───────────────────────────────────

export type QualityLevel = 'excellent' | 'good' | 'fair' | 'poor' | 'critical';

export interface PeerQualitySnapshot {
  peerId: string;
  rtt: number;           // ms
  jitter: number;        // seconds
  packetsLost: number;   // cumulative
  packetLossRate: number; // 0-1 over sample window
  bitrate: number;       // kbps
  level: QualityLevel;
  score: number;         // 0-100
}

export interface QualityChangeEvent {
  overallLevel: QualityLevel;
  overallScore: number;
  peerSnapshots: PeerQualitySnapshot[];
  appliedPreset: string;
  timestamp: number;
}

// ── Congestion Detection ─────────────────────────────

export interface CongestionState {
  isCongested: boolean;
  severity: 'none' | 'mild' | 'moderate' | 'severe';
  suggestedBitrateKbps: number;
  qualityLimitationReason: string;
  qualityLimitationDurations: Record<string, number>;
}

// ── Per-Track Quality ────────────────────────────────

export interface TrackQualityStats {
  trackId: string;
  maxBitrate: number;
  maxFramerate: number;
  currentBitrate: number;
  currentFramerate: number;
  fps: number;
  framesSent: number;
  timestamp: number;
}

// ── Quality History ─────────────────────────────────

export interface QualityHistoryEntry {
  timestamp: number;
  level: QualityLevel;
  score: number;
  preset: string;
}

// ── Quality Report ──────────────────────────────────

export interface QualityReport {
  callDurationMs: number;
  startTime: number;
  endTime: number;
  averageScore: number;
  minScore: number;
  maxScore: number;
  qualityLevelDistribution: Record<QualityLevel, number>;
  presetChanges: Array<{
    preset: string;
    timestamp: number;
    reason: string;
  }>;
  networkCharacteristics: {
    estimatedBandwidthKbps: number;
    avgRtt: number;
    avgJitter: number;
    avgPacketLossRate: number;
    detectedNetworkType: string;
  };
  congestionEvents: Array<{
    severity: string;
    duration: number;
    timestamp: number;
  }>;
  cpuBottlenecks: Array<{
    timestamp: number;
    estimatedLoad: number;
  }>;
  peerStatistics: Array<{
    peerId: string;
    avgScore: number;
    avgRtt: number;
    avgPacketLoss: number;
    avgBitrate: number;
  }>;
}

type QualityChangeCallback = (event: QualityChangeEvent) => void;
type AudioOnlyCallback = (active: boolean, reason: string) => void;

// ── Thresholds ──────────────────────────────────────

const SCORE_EXCELLENT = 90;
const SCORE_GOOD = 70;
const SCORE_FAIR = 50;
const SCORE_POOR = 30;

// How often to sample stats (ms)
const POLL_INTERVAL_MS = 3_000;

// Hysteresis: don't downgrade/upgrade too quickly
const UPGRADE_DELAY_MS = 10_000;   // Wait 10s of good quality before upgrading
const DOWNGRADE_DELAY_MS = 3_000;  // React quickly to degradation

// Sustained-quality thresholds for the audio-only fallback.
// Require N consecutive bad samples before forcing audio-only, and M consecutive
// good samples before auto-recovering video. This avoids flicker from transient
// Wi-Fi blips while still reacting inside ~10 seconds on real outages.
const AUDIO_ONLY_ENTER_STREAK = 3;   // 3 × POLL_INTERVAL_MS = 9 s of 'critical'
const AUDIO_ONLY_EXIT_STREAK = 5;    // 5 × POLL_INTERVAL_MS = 15 s of 'good+'

// Per-peer bitrate scaling for mesh
const MESH_BITRATE_SCALE: Record<number, number> = {
  1: 1.0,    // 1-to-1: full bitrate
  2: 0.85,   // 3 total participants
  3: 0.7,
  4: 0.6,
  5: 0.5,
  6: 0.45,
  7: 0.4,
  8: 0.35,
};

export class QualityController {
  private _pollTimer: ReturnType<typeof setInterval> | null = null;
  private _listeners: QualityChangeCallback[] = [];
  private _destroyed = false;

  // Current quality state
  private _currentPreset: string = 'lan-max';
  private _currentLevel: QualityLevel = 'excellent';
  private _lastUpgradeTime = 0;
  private _lastDowngradeTime = 0;

  // Audio-only fallback tracking
  private _audioOnlyFallbackEnabled = true;
  private _autoAudioOnlyActive = false;
  private _presetBeforeAudioOnly: string | null = null;
  private _criticalStreak = 0;
  private _goodStreak = 0;
  private _audioOnlyListeners: AudioOnlyCallback[] = [];

  // Previous stats for delta computation
  private _prevStats: Map<string, {
    packetsLost: number;
    packetsReceived: number;
    bytesSent: number;
    timestamp: number;
  }> = new Map();

  // Reference to peer connections (either single or group)
  private _singlePeer: PeerConnection | null = null;
  private _groupManager: GroupCallManager | null = null;

  // ── CPU & Congestion Detection ──────────────────────

  private _cpuOverloadDetected = false;
  private _prevEncoderStats: Map<string, {
    framesSent: number;
    timestamp: number;
  }> = new Map();

  // ── Custom Presets ──────────────────────────────────

  private _customPresets: Map<string, QualityPreset> = new Map();

  // ── Per-Track Quality Control ───────────────────────

  private _trackQualityLimits: Map<string, {
    maxBitrate: number;
    maxFramerate: number;
  }> = new Map();

  // ── Quality History & Trends ───────────────────────

  private _qualityHistory: QualityHistoryEntry[] = [];
  private readonly _maxHistoryEntries = 1000;

  // ── Bandwidth Reservation ──────────────────────────

  private _bandwidthReservations: Map<string, number> = new Map();

  // ── Network Characteristics ────────────────────────

  private _lastNetworkAnalysis: {
    estimatedBandwidthKbps: number;
    avgRtt: number;
    jitter: number;
    type: string;
    timestamp: number;
  } | null = null;

  // ── Server-side policy cap ─────────────────────────
  // Populated from GET /api/media-policy/me; every preset the controller
  // applies is clamped against this before hitting the wire. Null = no cap.
  private _serverCap: {
    maxWidth: number;
    maxHeight: number;
    maxFramerate: number;
    maxBitrateKbps: number;
    allow8k: boolean;
    allowClientOverride: boolean;
    enforceHardCap: boolean;
  } | null = null;
  private _allowedPresets: Set<string> | null = null;

  // ── Quality Report Tracking ────────────────────────

  private _reportStartTime = Date.now();
  private _reportAccumulator = {
    scores: [] as number[],
    presetChanges: [] as Array<{ preset: string; timestamp: number; reason: string }>,
    congestionEvents: [] as Array<{ severity: string; duration: number; timestamp: number }>,
    cpuBottlenecks: [] as Array<{ timestamp: number; estimatedLoad: number }>,
  };

  /**
   * Attach to a single PeerConnection (1-to-1 call).
   */
  attachSinglePeer(pc: PeerConnection): void {
    this._singlePeer = pc;
    this._groupManager = null;
  }

  /**
   * Attach to a GroupCallManager (group call).
   */
  attachGroup(group: GroupCallManager): void {
    this._groupManager = group;
    this._singlePeer = null;
  }

  /**
   * Start the quality monitoring loop.
   */
  start(): void {
    if (this._pollTimer) return;

    this._currentPreset = 'lan-max';
    this._currentLevel = 'excellent';
    this._prevStats.clear();
    this._criticalStreak = 0;
    this._goodStreak = 0;
    this._autoAudioOnlyActive = false;
    this._presetBeforeAudioOnly = null;
    this._reportStartTime = Date.now();
    this._reportAccumulator = {
      scores: [],
      presetChanges: [],
      congestionEvents: [],
      cpuBottlenecks: [],
    };

    this._pollTimer = setInterval(() => this._pollWithReporting(), POLL_INTERVAL_MS);
    console.log('[QualityCtrl] Started monitoring');
  }

  /**
   * Stop the quality monitoring loop.
   */
  stop(): void {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
    this._prevStats.clear();
    console.log('[QualityCtrl] Stopped monitoring');
  }

  get currentPreset(): string {
    return this._currentPreset;
  }

  get currentLevel(): QualityLevel {
    return this._currentLevel;
  }

  /**
   * Push the server-side media cap into the controller. Called once on
   * login (from MediaDeviceManager / AppBootstrap) and again whenever
   * the admin re-publishes the policy via socket event.
   *
   * Shape matches the `/api/media-policy/me` response.
   */
  setServerCap(cap: {
    max_width: number;
    max_height: number;
    max_framerate: number;
    max_bitrate_kbps: number;
    allow_8k: boolean;
    allow_client_override: boolean;
    enforce_hard_cap: boolean;
  }, ladder?: Array<{ id: string }>): void {
    this._serverCap = {
      maxWidth: cap.max_width,
      maxHeight: cap.max_height,
      maxFramerate: cap.max_framerate,
      maxBitrateKbps: cap.max_bitrate_kbps,
      allow8k: cap.allow_8k,
      allowClientOverride: cap.allow_client_override,
      enforceHardCap: cap.enforce_hard_cap,
    };
    if (ladder) {
      this._allowedPresets = new Set(ladder.map((r) => r.id));
    }

    // If the currently-applied preset exceeds the new cap, step down.
    const current = QUALITY_PRESETS[this._currentPreset];
    if (current && this._exceedsCap(current)) {
      const fallback = this._highestAllowedPreset();
      if (fallback) {
        console.log(`[QualityCtrl] Server cap tightened — stepping ${this._currentPreset} → ${fallback}`);
        void this.forcePreset(fallback);
      }
    }
  }

  /** Return the raw server cap (read-only copy). */
  getServerCap(): typeof this._serverCap {
    return this._serverCap ? { ...this._serverCap } : null;
  }

  /**
   * Return the preset list the current user is allowed to pick, ordered
   * high → low. UI feeds this directly to a dropdown.
   */
  getAllowedPresets(): Array<{ id: string; preset: QualityPreset }> {
    const ordered: Array<{ id: string; preset: QualityPreset }> = [];
    for (const id of PRESET_ORDER) {
      const preset = QUALITY_PRESETS[id];
      if (!preset) continue;
      if (id === '8k' && !(this._serverCap?.allow8k ?? false)) continue;
      if (this._allowedPresets && !this._allowedPresets.has(id) && id !== 'audio-only') continue;
      if (this._exceedsCap(preset)) continue;
      ordered.push({ id, preset });
    }
    return ordered;
  }

  private _exceedsCap(preset: QualityPreset): boolean {
    const cap = this._serverCap;
    if (!cap || !cap.enforceHardCap) return false;
    if (preset.idealWidth === 0 && preset.idealHeight === 0) return false; // audio-only always ok
    if (preset.idealWidth > cap.maxWidth) return true;
    if (preset.idealHeight > cap.maxHeight) return true;
    if (preset.maxFramerate > cap.maxFramerate) return true;
    if (preset.maxBitrateKbps > cap.maxBitrateKbps) return true;
    return false;
  }

  private _highestAllowedPreset(): string | null {
    const allowed = this.getAllowedPresets();
    return allowed.length > 0 ? allowed[0].id : null;
  }

  private _clampPreset(preset: QualityPreset): QualityPreset {
    const cap = this._serverCap;
    if (!cap || !cap.enforceHardCap) return preset;
    if (preset.idealWidth === 0 && preset.idealHeight === 0) return preset;
    return {
      label: preset.label,
      idealWidth: Math.min(preset.idealWidth, cap.maxWidth),
      idealHeight: Math.min(preset.idealHeight, cap.maxHeight),
      maxFramerate: Math.min(preset.maxFramerate, cap.maxFramerate),
      maxBitrateKbps: Math.min(preset.maxBitrateKbps, cap.maxBitrateKbps),
    };
  }

  /**
   * Force a specific quality preset (user override).
   */
  async forcePreset(presetName: string): Promise<void> {
    const raw = QUALITY_PRESETS[presetName];
    if (!raw) {
      console.warn(`[QualityCtrl] Unknown preset: ${presetName}`);
      return;
    }

    // Block presets the server policy disallows, unless the policy
    // explicitly permits client overrides.
    if (
      this._serverCap?.enforceHardCap
      && this._exceedsCap(raw)
      && !this._serverCap.allowClientOverride
    ) {
      console.warn(`[QualityCtrl] Preset ${presetName} blocked by server cap`);
      return;
    }

    const preset = this._clampPreset(raw);

    // A manual override cancels any in-progress auto audio-only fallback.
    // Streak counters are reset so the next bad patch restarts the clock.
    const wasAutoAudioOnly = this._autoAudioOnlyActive;
    this._autoAudioOnlyActive = false;
    this._presetBeforeAudioOnly = null;
    this._criticalStreak = 0;
    this._goodStreak = 0;

    this._currentPreset = presetName;
    await this._applyPreset(preset);
    if (wasAutoAudioOnly && presetName !== 'audio-only') {
      this._emitAudioOnly(false, 'user-override');
    }
    console.log(`[QualityCtrl] Forced preset: ${presetName}`);
  }

  /**
   * Subscribe to quality change events.
   */
  onChange(cb: QualityChangeCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter((l) => l !== cb);
    };
  }

  /**
   * Subscribe to audio-only fallback toggles. UI uses this to show a
   * "Video disabled — bad network" banner and an unmute-video button.
   */
  onAudioOnlyFallback(cb: AudioOnlyCallback): () => void {
    this._audioOnlyListeners.push(cb);
    return () => {
      this._audioOnlyListeners = this._audioOnlyListeners.filter((l) => l !== cb);
    };
  }

  /** Whether the automatic audio-only fallback is currently engaged. */
  get isAudioOnlyActive(): boolean {
    return this._autoAudioOnlyActive;
  }

  /**
   * Toggle the audio-only fallback feature. Disabling it mid-fallback
   * immediately restores the pre-fallback preset.
   */
  setAudioOnlyFallbackEnabled(enabled: boolean): void {
    this._audioOnlyFallbackEnabled = enabled;
    if (!enabled && this._autoAudioOnlyActive) {
      const restore = this._presetBeforeAudioOnly || 'medium';
      this._autoAudioOnlyActive = false;
      this._presetBeforeAudioOnly = null;
      this._criticalStreak = 0;
      this._goodStreak = 0;
      this._currentPreset = restore;
      this._applyPreset(QUALITY_PRESETS[restore]).catch((e) =>
        console.error('[QualityCtrl] Restore after disabling fallback failed:', e)
      );
      this._emitAudioOnly(false, 'fallback-disabled');
    }
  }

  private _emitAudioOnly(active: boolean, reason: string): void {
    for (const cb of this._audioOnlyListeners) {
      try { cb(active, reason); } catch (e) {
        console.error('[QualityCtrl] Audio-only listener error:', e);
      }
    }
  }

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;
    this.stop();
    this._listeners = [];
    this._audioOnlyListeners = [];
    this._singlePeer = null;
    this._groupManager = null;
    this._prevStats.clear();
    this._prevEncoderStats.clear();
    this._customPresets.clear();
    this._trackQualityLimits.clear();
    this._qualityHistory = [];
    this._bandwidthReservations.clear();
  }

  // ── Internal Polling ──────────────────────────────

  private async _poll(): Promise<void> {
    if (this._destroyed) return;

    try {
      const snapshots = await this._collectSnapshots();
      if (snapshots.length === 0) return;

      // Compute overall quality (worst peer determines the level)
      const worstScore = Math.min(...snapshots.map((s) => s.score));
      const overallLevel = this._scoreToLevel(worstScore);

      const now = Date.now();
      const event: QualityChangeEvent = {
        overallLevel,
        overallScore: worstScore,
        peerSnapshots: snapshots,
        appliedPreset: this._currentPreset,
        timestamp: now,
      };

      // Adaptive quality adjustment
      await this._adjustQuality(overallLevel, worstScore, now);

      // Update current level
      this._currentLevel = overallLevel;

      // Notify listeners
      for (const cb of this._listeners) {
        try {
          cb(event);
        } catch (e) {
          console.error('[QualityCtrl] Listener error:', e);
        }
      }
    } catch (e) {
      console.error('[QualityCtrl] Poll error:', e);
    }
  }

  private async _collectSnapshots(): Promise<PeerQualitySnapshot[]> {
    const snapshots: PeerQualitySnapshot[] = [];

    if (this._singlePeer && !this._singlePeer.destroyed) {
      const snap = await this._snapshotPeer('single', this._singlePeer);
      if (snap) snapshots.push(snap);
    }

    if (this._groupManager && !this._groupManager.destroyed) {
      for (const participant of this._groupManager.allParticipants) {
        if (participant.connection && !participant.connection.destroyed) {
          const snap = await this._snapshotPeer(
            participant.peerId,
            participant.connection
          );
          if (snap) snapshots.push(snap);
        }
      }
    }

    return snapshots;
  }

  private async _snapshotPeer(
    peerId: string,
    pc: PeerConnection
  ): Promise<PeerQualitySnapshot | null> {
    try {
      const stats = await pc.getStats();
      let rtt = 0;
      let jitter = 0;
      let packetsLost = 0;
      let packetsReceived = 0;
      let bytesSent = 0;

      stats.forEach((report: any) => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded') {
          rtt = (report.currentRoundTripTime || 0) * 1000; // to ms
        }
        if (report.type === 'inbound-rtp' && report.kind === 'video') {
          packetsLost = report.packetsLost || 0;
          packetsReceived = report.packetsReceived || 0;
          jitter = report.jitter || 0;
        }
        if (report.type === 'outbound-rtp' && report.kind === 'video') {
          bytesSent = report.bytesSent || 0;
        }
      });

      // Compute delta packet loss rate
      const prev = this._prevStats.get(peerId);
      let packetLossRate = 0;

      if (prev) {
        const deltaLost = packetsLost - prev.packetsLost;
        const deltaReceived = packetsReceived - prev.packetsReceived;
        const totalDelta = deltaLost + deltaReceived;
        packetLossRate = totalDelta > 0 ? deltaLost / totalDelta : 0;
      }

      // Compute bitrate (kbps) over interval
      let bitrate = 0;
      if (prev && prev.timestamp) {
        const dtSec = (Date.now() - prev.timestamp) / 1000;
        if (dtSec > 0) {
          bitrate = ((bytesSent - prev.bytesSent) * 8) / 1000 / dtSec;
        }
      }

      // Save for next delta
      this._prevStats.set(peerId, {
        packetsLost,
        packetsReceived,
        bytesSent,
        timestamp: Date.now(),
      });

      // Compute quality score (0-100)
      const score = this._computeScore(rtt, jitter, packetLossRate);
      const level = this._scoreToLevel(score);

      return {
        peerId,
        rtt,
        jitter,
        packetsLost,
        packetLossRate,
        bitrate: Math.max(0, bitrate),
        level,
        score,
      };
    } catch {
      return null;
    }
  }

  /**
   * Quality score formula — weighted combination of metrics.
   * Tuned for LAN: low RTT is expected, packet loss is the primary signal.
   */
  private _computeScore(rttMs: number, jitterSec: number, lossRate: number): number {
    let score = 100;

    // RTT penalty (LAN should be <5ms, anything >50ms is concerning)
    if (rttMs > 5) {
      score -= Math.min(30, (rttMs - 5) * 0.6);
    }

    // Jitter penalty (in seconds; >0.03s is noticeable)
    const jitterMs = jitterSec * 1000;
    if (jitterMs > 10) {
      score -= Math.min(25, (jitterMs - 10) * 0.5);
    }

    // Packet loss penalty (most impactful)
    if (lossRate > 0) {
      score -= Math.min(50, lossRate * 500); // 10% loss = -50
    }

    return Math.max(0, Math.round(score));
  }

  private _scoreToLevel(score: number): QualityLevel {
    if (score >= SCORE_EXCELLENT) return 'excellent';
    if (score >= SCORE_GOOD) return 'good';
    if (score >= SCORE_FAIR) return 'fair';
    if (score >= SCORE_POOR) return 'poor';
    return 'critical';
  }

  // ── Adaptive Adjustment ───────────────────────────

  private async _adjustQuality(
    level: QualityLevel,
    score: number,
    now: number
  ): Promise<void> {
    const presetOrder = ['audio-only', 'low', 'medium', 'high', 'lan-max'];
    const currentIdx = presetOrder.indexOf(this._currentPreset);

    // Scale bitrate by participant count
    const peerCount = this._groupManager
      ? this._groupManager.participantCount
      : this._singlePeer
        ? 1
        : 0;

    // ── Audio-only fallback ─────────────────────────────
    // Track consecutive-bad and consecutive-good samples independently so a
    // single blip doesn't force audio-only and a single lucky sample doesn't
    // pull us back out. Entry condition is strictly 'critical'; exit requires
    // 'good' or better (not just 'fair', which may still be choppy).
    if (this._audioOnlyFallbackEnabled) {
      if (level === 'critical') {
        this._criticalStreak += 1;
        this._goodStreak = 0;
      } else if (level === 'excellent' || level === 'good') {
        this._goodStreak += 1;
        this._criticalStreak = 0;
      } else {
        // 'poor' or 'fair' — ambiguous, freeze both counters so we don't
        // prematurely recover but also don't accumulate toward fallback.
        this._goodStreak = 0;
      }

      if (
        !this._autoAudioOnlyActive &&
        this._criticalStreak >= AUDIO_ONLY_ENTER_STREAK &&
        this._currentPreset !== 'audio-only'
      ) {
        this._presetBeforeAudioOnly = this._currentPreset;
        this._autoAudioOnlyActive = true;
        this._currentPreset = 'audio-only';
        this._lastDowngradeTime = now;
        await this._applyPreset(QUALITY_PRESETS['audio-only']);
        console.warn(
          `[QualityCtrl] Sustained critical quality (${this._criticalStreak} samples) ` +
          `— falling back to audio-only from '${this._presetBeforeAudioOnly}'`
        );
        this._emitAudioOnly(true, 'sustained-critical');
        return;
      }

      if (
        this._autoAudioOnlyActive &&
        this._goodStreak >= AUDIO_ONLY_EXIT_STREAK
      ) {
        const restorePreset = this._presetBeforeAudioOnly || 'medium';
        this._autoAudioOnlyActive = false;
        this._presetBeforeAudioOnly = null;
        // Step back one rung below the pre-fallback preset to avoid bouncing
        // back into critical territory the moment we restore.
        const restoreIdx = Math.max(1, presetOrder.indexOf(restorePreset) - 1);
        const safePreset = presetOrder[restoreIdx];
        this._currentPreset = safePreset;
        this._lastUpgradeTime = now;
        await this._applyPreset(QUALITY_PRESETS[safePreset]);
        console.log(
          `[QualityCtrl] Network recovered (${this._goodStreak} good samples) ` +
          `— restoring video at '${safePreset}'`
        );
        this._emitAudioOnly(false, 'recovered');
        return;
      }

      // While in audio-only, don't run the normal upgrade/downgrade ladder —
      // the streak counters own the restore decision.
      if (this._autoAudioOnlyActive) return;
    }

    // Determine target preset
    let targetIdx = currentIdx;

    if (level === 'critical' || level === 'poor') {
      // Downgrade
      if (now - this._lastDowngradeTime >= DOWNGRADE_DELAY_MS && currentIdx > 0) {
        targetIdx = Math.max(0, currentIdx - 1);
        this._lastDowngradeTime = now;
      }
    } else if (level === 'excellent' && score >= 95) {
      // Upgrade
      if (
        now - this._lastUpgradeTime >= UPGRADE_DELAY_MS &&
        currentIdx < presetOrder.length - 1
      ) {
        targetIdx = Math.min(presetOrder.length - 1, currentIdx + 1);
        this._lastUpgradeTime = now;
      }
    }

    if (targetIdx !== currentIdx) {
      const presetName = presetOrder[targetIdx];
      const preset = QUALITY_PRESETS[presetName];
      this._currentPreset = presetName;

      // Apply with mesh scaling
      const scale = MESH_BITRATE_SCALE[peerCount] ?? 0.3;
      const scaledBitrate = Math.round(preset.maxBitrateKbps * scale);

      await this._applyBitrateAndFramerate(
        scaledBitrate,
        preset.maxFramerate,
        preset.audioBitrateKbps,
      );

      console.log(
        `[QualityCtrl] Adjusted: ${presetOrder[currentIdx]} → ${presetName} ` +
        `(score=${score}, peers=${peerCount}, bitrate=${scaledBitrate}kbps, ` +
        `audio=${preset.audioBitrateKbps ?? DEFAULT_AUDIO_BITRATE_KBPS}kbps)`
      );
    }
  }

  private async _applyPreset(preset: QualityPreset): Promise<void> {
    const peerCount = this._groupManager
      ? this._groupManager.participantCount
      : 1;
    const scale = MESH_BITRATE_SCALE[peerCount] ?? 0.3;
    const scaledBitrate = Math.round(preset.maxBitrateKbps * scale);

    await this._applyBitrateAndFramerate(
      scaledBitrate,
      preset.maxFramerate,
      preset.audioBitrateKbps,
    );
  }

  private async _applyBitrateAndFramerate(
    bitrateKbps: number,
    framerate: number,
    audioBitrateKbps?: number
  ): Promise<void> {
    const audioKbps = audioBitrateKbps ?? DEFAULT_AUDIO_BITRATE_KBPS;

    if (this._singlePeer && !this._singlePeer.destroyed) {
      await this._singlePeer.setVideoBitrate(bitrateKbps);
      await this._singlePeer.setVideoFramerate(framerate);
      await this._singlePeer.setAudioBitrate(audioKbps);
    }

    if (this._groupManager && !this._groupManager.destroyed) {
      await this._groupManager.setVideoBitrateAll(bitrateKbps);
      await this._groupManager.setVideoFramerateAll(framerate);
      await this._groupManager.setAudioBitrateAll(audioKbps);
    }
  }

  // ──────────────────────────────────────────────────────
  // CPU USAGE ESTIMATION
  // ──────────────────────────────────────────────────────

  /**
   * Estimate CPU load based on encoder performance metrics.
   * Returns 0-100 where >75 indicates potential CPU bottleneck.
   */
  async estimateCpuLoad(): Promise<number> {
    try {
      const snapshots = await this._collectSnapshots();
      if (snapshots.length === 0) return 0;

      let maxLoad = 0;

      if (this._singlePeer && !this._singlePeer.destroyed) {
        const stats = await this._singlePeer.getStats();
        const load = this._analyzeCpuFromStats(stats, 'single');
        maxLoad = Math.max(maxLoad, load);
      }

      if (this._groupManager && !this._groupManager.destroyed) {
        for (const participant of this._groupManager.allParticipants) {
          if (participant.connection && !participant.connection.destroyed) {
            const stats = await participant.connection.getStats();
            const load = this._analyzeCpuFromStats(stats, participant.peerId);
            maxLoad = Math.max(maxLoad, load);
          }
        }
      }

      this._cpuOverloadDetected = maxLoad > 75;
      return maxLoad;
    } catch (e) {
      console.error('[QualityCtrl] CPU estimation error:', e);
      return 0;
    }
  }

  private _analyzeCpuFromStats(stats: RTCStatsReport, peerId: string): number {
    let framesSent = 0;
    let qualityLimitationReason = 'none';
    let qualityLimitationDurations: Record<string, number> = {};

    stats.forEach((report: any) => {
      if (report.type === 'outbound-rtp' && report.kind === 'video') {
        framesSent = report.framesSent || 0;
        qualityLimitationReason = report.qualityLimitation || 'none';
        qualityLimitationDurations = report.qualityLimitationDurations || {};
      }
    });

    // Detect CPU limitation patterns
    let cpuLoad = 0;

    if (qualityLimitationReason === 'cpu') {
      cpuLoad = 85;
    } else if (qualityLimitationReason === 'bandwidth') {
      cpuLoad = 40;
    }

    // Check if frame sending rate has degraded
    const prev = this._prevEncoderStats.get(peerId);
    if (prev) {
      const timeDeltaMs = Date.now() - prev.timestamp;
      const framesSentDelta = framesSent - prev.framesSent;
      const expectedFrames = (this._getPresetFramerate() * timeDeltaMs) / 1000;

      if (framesSentDelta < expectedFrames * 0.7 && qualityLimitationReason === 'cpu') {
        cpuLoad = Math.min(100, cpuLoad + 20);
      }
    }

    this._prevEncoderStats.set(peerId, { framesSent, timestamp: Date.now() });

    return cpuLoad;
  }

  private _getPresetFramerate(): number {
    const preset = QUALITY_PRESETS[this._currentPreset];
    return preset?.maxFramerate || 30;
  }

  // ──────────────────────────────────────────────────────
  // CONGESTION DETECTION
  // ──────────────────────────────────────────────────────

  /**
   * Detect congestion state based on packet loss and quality limitation.
   */
  async detectCongestion(): Promise<CongestionState> {
    try {
      const snapshots = await this._collectSnapshots();
      if (snapshots.length === 0) {
        return {
          isCongested: false,
          severity: 'none',
          suggestedBitrateKbps: (QUALITY_PRESETS[this._currentPreset]?.maxBitrateKbps || 5000),
          qualityLimitationReason: 'none',
          qualityLimitationDurations: {},
        };
      }

      const worstLoss = Math.max(...snapshots.map((s) => s.packetLossRate));
      let severity: 'none' | 'mild' | 'moderate' | 'severe' = 'none';
      let qualityLimitationReason = 'none';
      let qualityLimitationDurations: Record<string, number> = {};

      if (worstLoss > 0.1) severity = 'severe';
      else if (worstLoss > 0.05) severity = 'moderate';
      else if (worstLoss > 0.02) severity = 'mild';

      // Fetch quality limitation info from stats
      if (this._singlePeer && !this._singlePeer.destroyed) {
        const stats = await this._singlePeer.getStats();
        stats.forEach((report: any) => {
          if (report.type === 'outbound-rtp' && report.kind === 'video') {
            qualityLimitationReason = report.qualityLimitation || 'none';
            qualityLimitationDurations = report.qualityLimitationDurations || {};
          }
        });
      }

      // Suggest bitrate reduction if congested
      const currentBitrate = QUALITY_PRESETS[this._currentPreset]?.maxBitrateKbps || 5000;
      let suggestedBitrate = currentBitrate;

      if (severity === 'severe') {
        suggestedBitrate = Math.round(currentBitrate * 0.5);
      } else if (severity === 'moderate') {
        suggestedBitrate = Math.round(currentBitrate * 0.7);
      } else if (severity === 'mild') {
        suggestedBitrate = Math.round(currentBitrate * 0.85);
      }

      return {
        isCongested: severity !== 'none',
        severity,
        suggestedBitrateKbps: suggestedBitrate,
        qualityLimitationReason,
        qualityLimitationDurations,
      };
    } catch (e) {
      console.error('[QualityCtrl] Congestion detection error:', e);
      return {
        isCongested: false,
        severity: 'none',
        suggestedBitrateKbps: (QUALITY_PRESETS[this._currentPreset]?.maxBitrateKbps || 5000),
        qualityLimitationReason: 'none',
        qualityLimitationDurations: {},
      };
    }
  }

  // ──────────────────────────────────────────────────────
  // CUSTOM PRESET SUPPORT
  // ──────────────────────────────────────────────────────

  /**
   * Register a custom quality preset.
   */
  registerCustomPreset(key: string, preset: QualityPreset): void {
    if (key.startsWith('custom_')) {
      this._customPresets.set(key, preset);
      console.log(`[QualityCtrl] Registered custom preset: ${key}`);
    } else {
      console.warn(`[QualityCtrl] Custom preset key must start with 'custom_': ${key}`);
    }
  }

  /**
   * Remove a custom preset.
   */
  removeCustomPreset(key: string): void {
    if (this._customPresets.has(key)) {
      this._customPresets.delete(key);
      console.log(`[QualityCtrl] Removed custom preset: ${key}`);
    }
  }

  /**
   * Get list of all presets (built-in + custom).
   */
  getPresetList(): { key: string; label: string; isCustom: boolean }[] {
    const result: { key: string; label: string; isCustom: boolean }[] = [];

    // Built-in presets
    for (const [key, preset] of Object.entries(QUALITY_PRESETS)) {
      result.push({ key, label: preset.label, isCustom: false });
    }

    // Custom presets
    for (const [key, preset] of this._customPresets) {
      result.push({ key, label: preset.label, isCustom: true });
    }

    return result;
  }

  // ──────────────────────────────────────────────────────
  // PER-TRACK QUALITY CONTROL
  // ──────────────────────────────────────────────────────

  /**
   * Set quality limits for a specific media track.
   */
  async setTrackQuality(
    trackId: string,
    maxBitrate: number,
    maxFramerate?: number
  ): Promise<void> {
    this._trackQualityLimits.set(trackId, {
      maxBitrate,
      maxFramerate: maxFramerate ?? 30,
    });

    // Apply to sender if available
    if (this._singlePeer && !this._singlePeer.destroyed) {
      const senders = this._singlePeer.peerConnection.getSenders();
      for (const sender of senders) {
        if (sender.track?.id === trackId) {
          await this._singlePeer.setVideoBitrate(maxBitrate);
          if (maxFramerate) {
            await this._singlePeer.setVideoFramerate(maxFramerate);
          }
          break;
        }
      }
    }

    console.log(
      `[QualityCtrl] Set track quality: ${trackId} (${maxBitrate}kbps, ${maxFramerate || 30}fps)`
    );
  }

  /**
   * Get quality stats for a specific track.
   */
  async getTrackQualityStats(trackId: string): Promise<TrackQualityStats | null> {
    try {
      const limits = this._trackQualityLimits.get(trackId);
      if (!limits) return null;

      let fps = 0;
      let framesSent = 0;
      let currentBitrate = 0;

      if (this._singlePeer && !this._singlePeer.destroyed) {
        const stats = await this._singlePeer.getStats();
        stats.forEach((report: any) => {
          if (report.type === 'outbound-rtp' && report.kind === 'video') {
            fps = report.framesPerSecond || 0;
            framesSent = report.framesSent || 0;
            // Estimate bitrate from the stats
            const prev = this._prevStats.get(trackId);
            if (prev && prev.timestamp) {
              const dtSec = (Date.now() - prev.timestamp) / 1000;
              if (dtSec > 0) {
                currentBitrate = ((report.bytesSent - prev.bytesSent) * 8) / 1000 / dtSec;
              }
            }
          }
        });
      }

      return {
        trackId,
        maxBitrate: limits.maxBitrate,
        maxFramerate: limits.maxFramerate,
        currentBitrate: Math.max(0, currentBitrate),
        currentFramerate: fps,
        fps,
        framesSent,
        timestamp: Date.now(),
      };
    } catch (e) {
      console.error(`[QualityCtrl] Error getting track stats for ${trackId}:`, e);
      return null;
    }
  }

  // ──────────────────────────────────────────────────────
  // QUALITY HISTORY AND TRENDS
  // ──────────────────────────────────────────────────────

  /**
   * Get quality history for the past N milliseconds.
   */
  getQualityHistory(durationMs?: number): QualityHistoryEntry[] {
    if (!durationMs) return [...this._qualityHistory];

    const cutoffTime = Date.now() - durationMs;
    return this._qualityHistory.filter((entry) => entry.timestamp >= cutoffTime);
  }

  /**
   * Determine quality trend based on recent history.
   */
  getQualityTrend(): 'improving' | 'degrading' | 'stable' {
    if (this._qualityHistory.length < 3) return 'stable';

    const recentWindow = this._qualityHistory.slice(-10);
    const oldWindow = this._qualityHistory.slice(Math.max(0, this._qualityHistory.length - 20), -10);

    if (oldWindow.length === 0 || recentWindow.length === 0) return 'stable';

    const recentAvg = recentWindow.reduce((sum, e) => sum + e.score, 0) / recentWindow.length;
    const oldAvg = oldWindow.reduce((sum, e) => sum + e.score, 0) / oldWindow.length;

    const diff = recentAvg - oldAvg;
    if (Math.abs(diff) < 5) return 'stable';
    return diff > 0 ? 'improving' : 'degrading';
  }

  // ──────────────────────────────────────────────────────
  // BANDWIDTH RESERVATION
  // ──────────────────────────────────────────────────────

  /**
   * Reserve bandwidth for a specific use case (screen share, data channel, etc).
   */
  reserveBandwidth(label: string, kbps: number): void {
    this._bandwidthReservations.set(label, kbps);
    console.log(`[QualityCtrl] Reserved bandwidth: ${label} = ${kbps}kbps`);
  }

  /**
   * Release bandwidth reservation.
   */
  releaseBandwidth(label: string): void {
    this._bandwidthReservations.delete(label);
    console.log(`[QualityCtrl] Released bandwidth: ${label}`);
  }

  /**
   * Get available bandwidth after accounting for reservations.
   */
  getAvailableBandwidth(): number {
    const totalReserved = Array.from(this._bandwidthReservations.values()).reduce(
      (sum, kbps) => sum + kbps,
      0
    );

    const estimatedTotal = this._lastNetworkAnalysis?.estimatedBandwidthKbps || 10_000;
    return Math.max(0, estimatedTotal - totalReserved);
  }

  // ──────────────────────────────────────────────────────
  // NETWORK TYPE DETECTION
  // ──────────────────────────────────────────────────────

  /**
   * Detect network type from ICE candidates and RTT characteristics.
   */
  async detectNetworkType(): Promise<'ethernet' | 'wifi' | 'unknown'> {
    try {
      if (this._singlePeer && !this._singlePeer.destroyed) {
        const stats = await this._singlePeer.getStats();
        let hasHostCandidate = false;
        let bestRtt = Infinity;

        stats.forEach((report: any) => {
          if (report.type === 'candidate-pair' && report.state === 'succeeded') {
            const currentRtt = (report.currentRoundTripTime || 0) * 1000; // to ms
            bestRtt = Math.min(bestRtt, currentRtt);

            const local = report.localCandidate || {};
            const remote = report.remoteCandidate || {};

            if (
              (local.candidateType === 'host' || local.candidateType === 'srflx') &&
              (remote.candidateType === 'host' || remote.candidateType === 'srflx')
            ) {
              hasHostCandidate = true;
            }
          }
        });

        // Heuristics: wired connections typically have <2ms RTT in LAN
        if (bestRtt < 2 && hasHostCandidate) return 'ethernet';
        if (hasHostCandidate) return 'wifi';
      }

      return 'unknown';
    } catch (e) {
      console.error('[QualityCtrl] Network type detection error:', e);
      return 'unknown';
    }
  }

  /**
   * Get detailed network characteristics.
   */
  getNetworkCharacteristics(): {
    estimatedBandwidthKbps: number;
    avgRtt: number;
    jitter: number;
    type: string;
  } {
    if (this._lastNetworkAnalysis) {
      return {
        estimatedBandwidthKbps: this._lastNetworkAnalysis.estimatedBandwidthKbps,
        avgRtt: this._lastNetworkAnalysis.avgRtt,
        jitter: this._lastNetworkAnalysis.jitter,
        type: this._lastNetworkAnalysis.type,
      };
    }

    return {
      estimatedBandwidthKbps: 10_000,
      avgRtt: 0,
      jitter: 0,
      type: 'unknown',
    };
  }

  // ──────────────────────────────────────────────────────
  // DETAILED QUALITY REPORT
  // ──────────────────────────────────────────────────────

  /**
   * Generate comprehensive quality report for post-call analysis.
   */
  generateQualityReport(): QualityReport {
    const now = Date.now();
    const callDuration = now - this._reportStartTime;

    // Calculate score statistics
    const scores = this._reportAccumulator.scores;
    const avgScore = scores.length > 0 ? scores.reduce((a, b) => a + b, 0) / scores.length : 0;
    const minScore = scores.length > 0 ? Math.min(...scores) : 0;
    const maxScore = scores.length > 0 ? Math.max(...scores) : 0;

    // Quality level distribution
    const levelDist = this._qualityHistory.reduce(
      (acc, entry) => {
        acc[entry.level] = (acc[entry.level] || 0) + 1;
        return acc;
      },
      {} as Record<QualityLevel, number>
    );

    // Peer statistics
    const peerStats: Array<{
      peerId: string;
      avgScore: number;
      avgRtt: number;
      avgPacketLoss: number;
      avgBitrate: number;
    }> = [];

    // Aggregate per-peer data (simplified; in production would track per peer)
    const allPeerScores: number[] = [];
    const allRtts: number[] = [];
    const allLosses: number[] = [];

    // Network characteristics at last check
    const netChars = this.getNetworkCharacteristics();

    const report: QualityReport = {
      callDurationMs: callDuration,
      startTime: this._reportStartTime,
      endTime: now,
      averageScore: Math.round(avgScore),
      minScore: minScore,
      maxScore: maxScore,
      qualityLevelDistribution: levelDist,
      presetChanges: this._reportAccumulator.presetChanges,
      networkCharacteristics: {
        estimatedBandwidthKbps: netChars.estimatedBandwidthKbps,
        avgRtt: netChars.avgRtt,
        avgJitter: netChars.jitter,
        avgPacketLossRate: 0, // Could accumulate from snapshots
        detectedNetworkType: netChars.type,
      },
      congestionEvents: this._reportAccumulator.congestionEvents,
      cpuBottlenecks: this._reportAccumulator.cpuBottlenecks,
      peerStatistics: peerStats,
    };

    return report;
  }

  // ──────────────────────────────────────────────────────
  // INTERNAL POLLING EXTENSIONS
  // ──────────────────────────────────────────────────────

  /**
   * Override _poll to accumulate report data.
   */
  private async _pollWithReporting(): Promise<void> {
    if (this._destroyed) return;

    try {
      const snapshots = await this._collectSnapshots();
      if (snapshots.length === 0) return;

      // Compute overall quality
      const worstScore = Math.min(...snapshots.map((s) => s.score));
      const overallLevel = this._scoreToLevel(worstScore);

      // Accumulate for report
      this._reportAccumulator.scores.push(worstScore);
      this._qualityHistory.push({
        timestamp: Date.now(),
        level: overallLevel,
        score: worstScore,
        preset: this._currentPreset,
      });

      // Keep history size reasonable
      if (this._qualityHistory.length > this._maxHistoryEntries) {
        this._qualityHistory.shift();
      }

      const now = Date.now();
      const event: QualityChangeEvent = {
        overallLevel,
        overallScore: worstScore,
        peerSnapshots: snapshots,
        appliedPreset: this._currentPreset,
        timestamp: now,
      };

      // Update network analysis
      if (snapshots.length > 0) {
        const avgRtt = snapshots.reduce((sum, s) => sum + s.rtt, 0) / snapshots.length;
        const avgJitter = snapshots.reduce((sum, s) => sum + s.jitter, 0) / snapshots.length;
        const totalBitrate = snapshots.reduce((sum, s) => sum + s.bitrate, 0);

        this._lastNetworkAnalysis = {
          estimatedBandwidthKbps: Math.round(totalBitrate * 1.2), // add headroom
          avgRtt,
          jitter: avgJitter,
          type: (await this.detectNetworkType()) || 'unknown',
          timestamp: now,
        };
      }

      // Adaptive quality adjustment
      await this._adjustQuality(overallLevel, worstScore, now);

      // Update current level
      this._currentLevel = overallLevel;

      // Notify listeners
      for (const cb of this._listeners) {
        try {
          cb(event);
        } catch (e) {
          console.error('[QualityCtrl] Listener error:', e);
        }
      }
    } catch (e) {
      console.error('[QualityCtrl] Poll error:', e);
    }
  }
}
