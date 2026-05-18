/**
 * GroupCallResourceManager.ts — Per-participant budgets & CPU spike prevention.
 *
 * Identified problems:
 *   1. Adding participants causes resource spikes (new PeerConnection + codec init)
 *   2. No per-participant resource budgeting (all get equal allocation)
 *   3. Active speaker detection triggers layout recalculation + re-render
 *   4. Multiple video decoders compete for GPU (decode queue overflow)
 *   5. ICE candidate storms when multiple peers connect simultaneously
 *   6. No staggered negotiation (all offers/answers fire at once)
 *
 * Solutions:
 *   1. Staggered participant connection (500ms between each new peer)
 *   2. Per-participant bitrate budget based on total available bandwidth / N
 *   3. Priority-based video allocation (speaker gets more, others thumbnail)
 *   4. Maximum simultaneous video decoders cap
 *   5. ICE candidate batching (collect for 100ms then send all)
 *   6. Gradual quality ramp-up (start low, increase after connection stabilizes)
 *
 * Integrates with:
 *   - ResourceGovernor (severity feeds)
 *   - MediaBudgetController (per-participant allocation)
 *   - GroupCallOptimizer (existing, provides speaker detection)
 */

import type { GovernorSeverity } from '../performance/ResourceGovernor';
import type { DeviceTier } from '../performance/DeviceCapabilityDetector';

// ── Types ───────────────────────────────────────────────────

export interface ParticipantResourceBudget {
  participantId: string;
  /** Whether this participant is the active speaker */
  isSpeaker: boolean;
  /** Whether this participant's video should be enabled */
  videoEnabled: boolean;
  /** Max video width for this participant */
  maxVideoWidth: number;
  /** Max video height for this participant */
  maxVideoHeight: number;
  /** Max video FPS */
  maxVideoFps: number;
  /** Max video bitrate (kbps) */
  maxVideoBitrateKbps: number;
  /** Max audio bitrate (kbps) */
  maxAudioBitrateKbps: number;
  /** Priority tier: 'speaker' | 'visible' | 'offscreen' */
  priority: 'speaker' | 'visible' | 'offscreen';
  /** Connection phase: 'pending' | 'connecting' | 'ramping' | 'stable' */
  connectionPhase: 'pending' | 'connecting' | 'ramping' | 'stable';
}

export interface GroupResourceAllocation {
  /** Total available bandwidth for the call (kbps) */
  totalBandwidthKbps: number;
  /** Bandwidth reserved for audio (kbps) */
  audioBandwidthKbps: number;
  /** Bandwidth available for video (kbps) */
  videoBandwidthKbps: number;
  /** Per-participant budgets */
  budgets: ParticipantResourceBudget[];
  /** Maximum simultaneous video decoders */
  maxVideoDecoders: number;
  /** Number of participants with video enabled */
  videoEnabledCount: number;
  /** Stagger delay between connecting new peers (ms) */
  connectionStaggerMs: number;
  /** ICE candidate batch window (ms) */
  iceBatchWindowMs: number;
  /** Quality ramp-up duration after stable connection (ms) */
  qualityRampDurationMs: number;
}

export interface ICECandidateBatch {
  peerId: string;
  candidates: RTCIceCandidate[];
  timestamp: number;
}

type AllocationCallback = (allocation: GroupResourceAllocation) => void;
type ICEBatchCallback = (batch: ICECandidateBatch) => void;

// ── Constants ───────────────────────────────────────────────

/** Delay between connecting new participants */
const CONNECTION_STAGGER_MS = 500;

/** ICE candidate batching window */
const ICE_BATCH_WINDOW_MS = 100;

/** Time to ramp up quality after stable connection */
const QUALITY_RAMP_MS = 3_000;

/** Minimum audio budget per participant (kbps) */
const MIN_AUDIO_PER_PARTICIPANT_KBPS = 32;

/** Speaker video budget multiplier */
const SPEAKER_VIDEO_MULTIPLIER = 2.5;

/** Visible (non-speaker) video budget multiplier */
const VISIBLE_VIDEO_MULTIPLIER = 1.0;

/** Maximum simultaneous video decoders per tier */
const MAX_DECODERS: Record<DeviceTier, number> = {
  minimal: 1,
  low: 2,
  medium: 4,
  high: 8,
};

/** Total bandwidth budget per tier (kbps) */
const BANDWIDTH_BUDGET: Record<DeviceTier, number> = {
  minimal: 1_000,
  low: 3_000,
  medium: 8_000,
  high: 20_000,
};

// ── GroupCallResourceManager ────────────────────────────────

export class GroupCallResourceManager {
  private _deviceTier: DeviceTier = 'medium';
  private _severity: GovernorSeverity = 0;
  private _participants = new Map<string, {
    isSpeaker: boolean;
    isVideoOn: boolean;
    connectionPhase: ParticipantResourceBudget['connectionPhase'];
    connectedAt: number;
    rampTimer: ReturnType<typeof setTimeout> | null;
  }>();

  private _allocationListeners: AllocationCallback[] = [];
  private _iceBatchListeners: ICEBatchCallback[] = [];
  private _iceBatchBuffers = new Map<string, {
    candidates: RTCIceCandidate[];
    timer: ReturnType<typeof setTimeout> | null;
  }>();

  // Connection stagger queue
  private _connectionQueue: Array<{ peerId: string; connect: () => void }> = [];
  private _connectionTimer: ReturnType<typeof setTimeout> | null = null;
  private _isProcessingQueue = false;

  private _destroyed = false;

  // ── Configuration ─────────────────────────────────────────

  setDeviceTier(tier: DeviceTier): void {
    this._deviceTier = tier;
    this._recompute();
  }

  feedSeverity(severity: GovernorSeverity): void {
    if (severity !== this._severity) {
      this._severity = severity;
      this._recompute();
    }
  }

  // ── Participant Management ────────────────────────────────

  /**
   * Add a participant. Connection will be staggered (not immediate).
   */
  addParticipant(peerId: string, connect: () => void): void {
    if (this._destroyed) return;

    this._participants.set(peerId, {
      isSpeaker: false,
      isVideoOn: false,
      connectionPhase: 'pending',
      connectedAt: 0,
      rampTimer: null,
    });

    // Queue for staggered connection
    this._connectionQueue.push({ peerId, connect });

    if (!this._isProcessingQueue) {
      this._processConnectionQueue();
    }
  }

  /**
   * Remove a participant and free their resources.
   */
  removeParticipant(peerId: string): void {
    const p = this._participants.get(peerId);
    if (p?.rampTimer) clearTimeout(p.rampTimer);

    this._participants.delete(peerId);

    // Remove from connection queue if pending
    this._connectionQueue = this._connectionQueue.filter(q => q.peerId !== peerId);

    // Clean up ICE batch
    const batch = this._iceBatchBuffers.get(peerId);
    if (batch?.timer) clearTimeout(batch.timer);
    this._iceBatchBuffers.delete(peerId);

    this._recompute();
  }

  /**
   * Mark a participant as the active speaker.
   */
  setSpeaker(peerId: string): void {
    for (const [id, p] of this._participants) {
      p.isSpeaker = id === peerId;
    }
    this._recompute();
  }

  /**
   * Update a participant's video state.
   */
  setVideoState(peerId: string, isVideoOn: boolean): void {
    const p = this._participants.get(peerId);
    if (p) {
      p.isVideoOn = isVideoOn;
      this._recompute();
    }
  }

  /**
   * Mark a participant's connection as stable.
   * Triggers quality ramp-up.
   */
  markStable(peerId: string): void {
    const p = this._participants.get(peerId);
    if (!p) return;

    p.connectionPhase = 'ramping';
    p.connectedAt = Date.now();

    // Start quality ramp timer
    p.rampTimer = setTimeout(() => {
      p.connectionPhase = 'stable';
      p.rampTimer = null;
      this._recompute();
    }, QUALITY_RAMP_MS);

    this._recompute();
  }

  // ── ICE Candidate Batching ────────────────────────────────

  /**
   * Buffer an ICE candidate for batched sending.
   */
  bufferICECandidate(peerId: string, candidate: RTCIceCandidate): void {
    if (this._destroyed) return;

    let buffer = this._iceBatchBuffers.get(peerId);
    if (!buffer) {
      buffer = { candidates: [], timer: null };
      this._iceBatchBuffers.set(peerId, buffer);
    }

    buffer.candidates.push(candidate);

    // Start batch timer if not already running
    if (!buffer.timer) {
      buffer.timer = setTimeout(() => {
        this._flushICEBatch(peerId);
      }, ICE_BATCH_WINDOW_MS);
    }
  }

  // ── Event Subscription ────────────────────────────────────

  onAllocation(cb: AllocationCallback): () => void {
    this._allocationListeners.push(cb);
    return () => {
      this._allocationListeners = this._allocationListeners.filter(l => l !== cb);
    };
  }

  onICEBatch(cb: ICEBatchCallback): () => void {
    this._iceBatchListeners.push(cb);
    return () => {
      this._iceBatchListeners = this._iceBatchListeners.filter(l => l !== cb);
    };
  }

  // ── Get Current Allocation ────────────────────────────────

  getAllocation(): GroupResourceAllocation {
    return this._buildAllocation();
  }

  /**
   * Get the budget for a specific participant.
   */
  getParticipantBudget(peerId: string): ParticipantResourceBudget | null {
    const alloc = this._buildAllocation();
    return alloc.budgets.find(b => b.participantId === peerId) ?? null;
  }

  // ── Lifecycle ─────────────────────────────────────────────

  destroy(): void {
    this._destroyed = true;

    // Clear all timers
    if (this._connectionTimer) clearTimeout(this._connectionTimer);
    for (const [, p] of this._participants) {
      if (p.rampTimer) clearTimeout(p.rampTimer);
    }
    for (const [, b] of this._iceBatchBuffers) {
      if (b.timer) clearTimeout(b.timer);
    }

    this._participants.clear();
    this._connectionQueue = [];
    this._iceBatchBuffers.clear();
    this._allocationListeners = [];
    this._iceBatchListeners = [];
  }

  // ── Internal: Connection Queue ────────────────────────────

  private _processConnectionQueue(): void {
    if (this._destroyed || this._connectionQueue.length === 0) {
      this._isProcessingQueue = false;
      return;
    }

    this._isProcessingQueue = true;

    const next = this._connectionQueue.shift()!;
    const p = this._participants.get(next.peerId);
    if (p) {
      p.connectionPhase = 'connecting';
    }

    // Execute connection
    try {
      next.connect();
    } catch (err) {
      console.warn(`[GroupCallResourceManager] Connection failed for ${next.peerId}`, err);
    }

    // Stagger next connection
    if (this._connectionQueue.length > 0) {
      const staggerMs = this._severity >= 3
        ? CONNECTION_STAGGER_MS * 2  // Double stagger under heavy load
        : CONNECTION_STAGGER_MS;

      this._connectionTimer = setTimeout(() => {
        this._processConnectionQueue();
      }, staggerMs);
    } else {
      this._isProcessingQueue = false;
    }

    this._recompute();
  }

  // ── Internal: ICE Batch Flush ─────────────────────────────

  private _flushICEBatch(peerId: string): void {
    const buffer = this._iceBatchBuffers.get(peerId);
    if (!buffer || buffer.candidates.length === 0) return;

    buffer.timer = null;

    const batch: ICECandidateBatch = {
      peerId,
      candidates: [...buffer.candidates],
      timestamp: Date.now(),
    };

    buffer.candidates = [];

    for (const cb of this._iceBatchListeners) {
      try { cb(batch); } catch {}
    }
  }

  // ── Internal: Allocation Computation ──────────────────────

  private _recompute(): void {
    if (this._destroyed) return;

    const allocation = this._buildAllocation();

    for (const cb of this._allocationListeners) {
      try { cb(allocation); } catch {}
    }
  }

  private _buildAllocation(): GroupResourceAllocation {
    const tier = this._deviceTier;
    const participantCount = this._participants.size;
    const maxDecoders = MAX_DECODERS[tier];

    // Reduce bandwidth under pressure
    let totalBandwidth = BANDWIDTH_BUDGET[tier];
    if (this._severity >= 4) totalBandwidth *= 0.25;
    else if (this._severity >= 3) totalBandwidth *= 0.5;
    else if (this._severity >= 2) totalBandwidth *= 0.75;

    // Reserve audio bandwidth (never compromised)
    const audioBandwidth = participantCount * MIN_AUDIO_PER_PARTICIPANT_KBPS;
    const videoBandwidth = Math.max(0, totalBandwidth - audioBandwidth);

    // Count participants with video
    let speakerCount = 0;
    let visibleCount = 0;
    for (const [, p] of this._participants) {
      if (p.isSpeaker && p.isVideoOn) speakerCount++;
      else if (p.isVideoOn) visibleCount++;
    }

    // Determine how many can have video active
    const maxVideo = Math.min(maxDecoders, speakerCount + visibleCount);

    // Compute per-participant budgets
    const budgets: ParticipantResourceBudget[] = [];

    // Distribute video bandwidth
    const totalVideoUnits =
      (speakerCount * SPEAKER_VIDEO_MULTIPLIER) +
      (Math.min(visibleCount, maxVideo - speakerCount) * VISIBLE_VIDEO_MULTIPLIER);

    const perUnitKbps = totalVideoUnits > 0 ? videoBandwidth / totalVideoUnits : 0;

    let videoSlotsRemaining = maxVideo;

    for (const [id, p] of this._participants) {
      let priority: ParticipantResourceBudget['priority'] = 'offscreen';
      let videoEnabled = false;
      let videoBudgetKbps = 0;
      let videoWidth = 0;
      let videoHeight = 0;
      let videoFps = 0;

      if (p.isSpeaker && p.isVideoOn && videoSlotsRemaining > 0) {
        priority = 'speaker';
        videoEnabled = true;
        videoBudgetKbps = Math.round(perUnitKbps * SPEAKER_VIDEO_MULTIPLIER);
        videoSlotsRemaining--;
      } else if (p.isVideoOn && videoSlotsRemaining > 0) {
        priority = 'visible';
        videoEnabled = true;
        videoBudgetKbps = Math.round(perUnitKbps * VISIBLE_VIDEO_MULTIPLIER);
        videoSlotsRemaining--;
      }

      // Map bitrate to resolution
      if (videoEnabled) {
        const resolved = this._bitrateToResolution(videoBudgetKbps, priority);
        videoWidth = resolved.width;
        videoHeight = resolved.height;
        videoFps = resolved.fps;
      }

      // Apply ramp-up factor
      if (p.connectionPhase === 'ramping') {
        videoBudgetKbps = Math.round(videoBudgetKbps * 0.5);
        videoFps = Math.min(videoFps, 15);
      } else if (p.connectionPhase === 'connecting') {
        videoEnabled = false;
      }

      budgets.push({
        participantId: id,
        isSpeaker: p.isSpeaker,
        videoEnabled,
        maxVideoWidth: videoWidth,
        maxVideoHeight: videoHeight,
        maxVideoFps: videoFps,
        maxVideoBitrateKbps: videoBudgetKbps,
        maxAudioBitrateKbps: MIN_AUDIO_PER_PARTICIPANT_KBPS,
        priority,
        connectionPhase: p.connectionPhase,
      });
    }

    return {
      totalBandwidthKbps: totalBandwidth,
      audioBandwidthKbps: audioBandwidth,
      videoBandwidthKbps: videoBandwidth,
      budgets,
      maxVideoDecoders: maxDecoders,
      videoEnabledCount: budgets.filter(b => b.videoEnabled).length,
      connectionStaggerMs: this._severity >= 3 ? CONNECTION_STAGGER_MS * 2 : CONNECTION_STAGGER_MS,
      iceBatchWindowMs: ICE_BATCH_WINDOW_MS,
      qualityRampDurationMs: QUALITY_RAMP_MS,
    };
  }

  private _bitrateToResolution(bitrateKbps: number, priority: 'speaker' | 'visible' | 'offscreen'): {
    width: number;
    height: number;
    fps: number;
  } {
    if (bitrateKbps >= 3000 && priority === 'speaker') {
      return { width: 1280, height: 720, fps: 30 };
    }
    if (bitrateKbps >= 1500) {
      return { width: 720, height: 480, fps: 24 };
    }
    if (bitrateKbps >= 750) {
      return { width: 480, height: 360, fps: 20 };
    }
    if (bitrateKbps >= 300) {
      return { width: 320, height: 240, fps: 15 };
    }
    return { width: 160, height: 120, fps: 10 };
  }
}

// ── Singleton ───────────────────────────────────────────────

export const groupCallResourceManager = new GroupCallResourceManager();
