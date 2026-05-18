/**
 * MediaBudgetController.ts — Audio-priority media allocation engine.
 *
 * Core principle: AUDIO IS KING. On resource-constrained devices, the user
 * must always be able to hear and be heard. Video is a luxury that gets
 * progressively reduced or disabled before audio quality is touched.
 *
 * Allocation hierarchy (highest to lowest priority):
 *   1. Outgoing audio (microphone)     — NEVER degraded below 16kbps
 *   2. Incoming audio (remote peers)   — NEVER degraded below 16kbps each
 *   3. Incoming video (active speaker) — reduced first in resolution, then FPS
 *   4. Outgoing video (camera)         — reduced aggressively under pressure
 *   5. Screen share (send/receive)     — reduced to low FPS still-image mode
 *   6. Non-active speaker video        — disabled entirely under pressure
 *
 * Integration:
 *   - Reads MediaBudget from HardwareProfiles (via AutoPerformanceManager)
 *   - Accepts pressure signals from ResourceGovernor
 *   - Outputs MediaConstraints compatible with WebRTC applyConstraints()
 *   - Outputs track enable/disable commands for peer connections
 *
 * Does NOT directly manipulate WebRTC tracks. Emits constraint objects that
 * the CallEngine / PeerManager applies.
 */

import type { MediaBudget } from './HardwareProfiles';
import type { GovernorSeverity } from './ResourceGovernor';

// ── Types ───────────────────────────────────────────────────

export interface MediaAllocation {
  /** Audio track constraints */
  audio: AudioAllocation;
  /** Video track constraints */
  video: VideoAllocation;
  /** Screen share constraints */
  screenShare: ScreenShareAllocation;
  /** Per-peer incoming video policy */
  incomingVideo: IncomingVideoPolicy;
  /** Severity that produced this allocation */
  severity: GovernorSeverity;
  /** Timestamp of last recomputation */
  timestamp: number;
}

export interface AudioAllocation {
  enabled: boolean;
  sampleRate: number;
  maxBitrateKbps: number;
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
}

export interface VideoAllocation {
  enabled: boolean;
  maxWidth: number;
  maxHeight: number;
  maxFps: number;
  maxBitrateKbps: number;
  /** Reason if disabled */
  disableReason?: string;
}

export interface ScreenShareAllocation {
  enabled: boolean;
  maxWidth: number;
  maxFps: number;
  maxBitrateKbps: number;
}

export interface IncomingVideoPolicy {
  /** Maximum peers to show video for simultaneously */
  maxVisibleVideos: number;
  /** Only show video for active speaker */
  activeSpeakerOnly: boolean;
  /** Max resolution to request from remote peers (SDP bandwidth) */
  maxIncomingWidth: number;
  /** Max incoming FPS to request */
  maxIncomingFps: number;
}

type AllocationCallback = (allocation: MediaAllocation) => void;

// ── Constants ───────────────────────────────────────────────

/** Absolute minimum audio bitrate — never go below this */
const AUDIO_FLOOR_KBPS = 16;

/** Minimum audio sample rate */
const AUDIO_FLOOR_SAMPLE_RATE = 8_000;

/** Video bitrate steps for progressive degradation (kbps) */
const VIDEO_BITRATE_STEPS = [8000, 4000, 2000, 1000, 500, 250, 0];

/** Video resolution steps (width) */
const VIDEO_RESOLUTION_STEPS = [1920, 1280, 720, 480, 360, 0];

/** Video FPS steps */
const VIDEO_FPS_STEPS = [30, 24, 15, 10, 5, 0];

// ── MediaBudgetController ───────────────────────────────────

export class MediaBudgetController {
  private _budget: MediaBudget;
  private _severity: GovernorSeverity = 0;
  private _currentAllocation: MediaAllocation | null = null;
  private _listeners: AllocationCallback[] = [];
  private _destroyed = false;

  // External state feeds
  private _isInCall = false;
  private _activePeerCount = 0;
  private _isScreenSharing = false;

  constructor(budget: MediaBudget) {
    this._budget = budget;
  }

  // ── Lifecycle ─────────────────────────────────────────────

  destroy(): void {
    this._destroyed = true;
    this._listeners = [];
  }

  // ── Configuration ─────────────────────────────────────────

  updateBudget(budget: MediaBudget): void {
    this._budget = budget;
    this._recompute();
  }

  // ── External Feeds ────────────────────────────────────────

  /**
   * Feed severity from ResourceGovernor.
   */
  feedSeverity(severity: GovernorSeverity): void {
    if (severity !== this._severity) {
      this._severity = severity;
      this._recompute();
    }
  }

  /**
   * Feed call state.
   */
  feedCallState(isInCall: boolean, peerCount: number, isScreenSharing: boolean): void {
    this._isInCall = isInCall;
    this._activePeerCount = peerCount;
    this._isScreenSharing = isScreenSharing;
    this._recompute();
  }

  // ── Event Subscription ────────────────────────────────────

  on(cb: AllocationCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  // ── Get Current Allocation ────────────────────────────────

  getAllocation(): MediaAllocation {
    if (!this._currentAllocation) {
      this._recompute();
    }
    return this._currentAllocation!;
  }

  /**
   * Get WebRTC-compatible constraints for getUserMedia().
   */
  getMediaConstraints(): { audio: MediaTrackConstraints; video: MediaTrackConstraints | false } {
    const alloc = this.getAllocation();

    const audio: MediaTrackConstraints = {
      sampleRate: { ideal: alloc.audio.sampleRate },
      echoCancellation: { ideal: alloc.audio.echoCancellation },
      noiseSuppression: { ideal: alloc.audio.noiseSuppression },
      autoGainControl: { ideal: alloc.audio.autoGainControl },
    };

    if (!alloc.video.enabled) {
      return { audio, video: false };
    }

    const video: MediaTrackConstraints = {
      width: { ideal: alloc.video.maxWidth, max: alloc.video.maxWidth },
      height: { ideal: alloc.video.maxHeight, max: alloc.video.maxHeight },
      frameRate: { ideal: alloc.video.maxFps, max: alloc.video.maxFps },
    };

    return { audio, video };
  }

  /**
   * Get screen share constraints for getDisplayMedia().
   */
  getScreenShareConstraints(): MediaTrackConstraints | false {
    const alloc = this.getAllocation();
    if (!alloc.screenShare.enabled) return false;

    return {
      width: { ideal: alloc.screenShare.maxWidth, max: alloc.screenShare.maxWidth },
      frameRate: { ideal: alloc.screenShare.maxFps, max: alloc.screenShare.maxFps },
    };
  }

  // ── Internal: Recompute Allocation ────────────────────────

  private _recompute(): void {
    if (this._destroyed) return;

    const b = this._budget;
    const s = this._severity;

    // ── Audio: always maintained, only touch at emergency ───
    const audio = this._computeAudioAllocation(b, s);

    // ── Video: progressively degraded ──────────────────────
    const video = this._computeVideoAllocation(b, s);

    // ── Screen share: aggressively reduced ─────────────────
    const screenShare = this._computeScreenShareAllocation(b, s);

    // ── Incoming video policy ──────────────────────────────
    const incomingVideo = this._computeIncomingVideoPolicy(b, s);

    const allocation: MediaAllocation = {
      audio,
      video,
      screenShare,
      incomingVideo,
      severity: s,
      timestamp: Date.now(),
    };

    this._currentAllocation = allocation;
    this._emit(allocation);
  }

  private _computeAudioAllocation(b: MediaBudget, s: GovernorSeverity): AudioAllocation {
    // Audio is ALWAYS enabled during calls
    let maxBitrateKbps = b.maxAudioBitrateKbps;
    let sampleRate = b.audioSampleRate;

    // Only reduce audio at severity 4 (emergency)
    if (s >= 4) {
      maxBitrateKbps = Math.max(AUDIO_FLOOR_KBPS, Math.floor(b.maxAudioBitrateKbps * 0.5));
      sampleRate = Math.max(AUDIO_FLOOR_SAMPLE_RATE, 16_000);
    } else if (s >= 3) {
      // Slight reduction
      maxBitrateKbps = Math.max(AUDIO_FLOOR_KBPS, Math.floor(b.maxAudioBitrateKbps * 0.75));
    }

    return {
      enabled: true,
      sampleRate,
      maxBitrateKbps,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    };
  }

  private _computeVideoAllocation(b: MediaBudget, s: GovernorSeverity): VideoAllocation {
    // Not in a call: no video needed
    if (!this._isInCall) {
      return this._disabledVideo('Not in a call');
    }

    // Severity 4: disable outgoing video entirely
    if (s >= 4) {
      return this._disabledVideo('Emergency: resources critical, audio only');
    }

    // Severity 3: minimal video
    if (s >= 3) {
      return {
        enabled: true,
        maxWidth: Math.min(b.maxVideoWidth, 360),
        maxHeight: Math.min(b.maxVideoHeight, 270),
        maxFps: Math.min(b.maxVideoFps, 10),
        maxBitrateKbps: Math.min(b.maxVideoBitrateKbps, 250),
      };
    }

    // Severity 2: reduced video
    if (s >= 2) {
      return {
        enabled: true,
        maxWidth: Math.min(b.maxVideoWidth, 480),
        maxHeight: Math.min(b.maxVideoHeight, 360),
        maxFps: Math.min(b.maxVideoFps, 15),
        maxBitrateKbps: Math.min(b.maxVideoBitrateKbps, 500),
      };
    }

    // Severity 1: slightly reduced
    if (s >= 1) {
      return {
        enabled: true,
        maxWidth: Math.min(b.maxVideoWidth, 640),
        maxHeight: Math.min(b.maxVideoHeight, 480),
        maxFps: Math.min(b.maxVideoFps, 24),
        maxBitrateKbps: Math.min(b.maxVideoBitrateKbps, 1000),
      };
    }

    // Nominal: use budget as-is
    return {
      enabled: true,
      maxWidth: b.maxVideoWidth,
      maxHeight: b.maxVideoHeight,
      maxFps: b.maxVideoFps,
      maxBitrateKbps: b.maxVideoBitrateKbps,
    };
  }

  private _computeScreenShareAllocation(b: MediaBudget, s: GovernorSeverity): ScreenShareAllocation {
    if (!this._isScreenSharing) {
      return { enabled: false, maxWidth: 0, maxFps: 0, maxBitrateKbps: 0 };
    }

    // Severity 4: disable screen share
    if (s >= 4) {
      return { enabled: false, maxWidth: 0, maxFps: 0, maxBitrateKbps: 0 };
    }

    // Severity 3: still-image mode (1 FPS)
    if (s >= 3) {
      return {
        enabled: true,
        maxWidth: Math.min(b.maxScreenShareWidth, 1280),
        maxFps: 1,
        maxBitrateKbps: 500,
      };
    }

    // Severity 2: low FPS
    if (s >= 2) {
      return {
        enabled: true,
        maxWidth: Math.min(b.maxScreenShareWidth, 1280),
        maxFps: Math.min(b.maxScreenShareFps, 5),
        maxBitrateKbps: 1000,
      };
    }

    // Severity 1: slightly reduced
    if (s >= 1) {
      return {
        enabled: true,
        maxWidth: b.maxScreenShareWidth,
        maxFps: Math.min(b.maxScreenShareFps, 10),
        maxBitrateKbps: 2000,
      };
    }

    // Nominal
    return {
      enabled: true,
      maxWidth: b.maxScreenShareWidth,
      maxFps: b.maxScreenShareFps,
      maxBitrateKbps: 4000,
    };
  }

  private _computeIncomingVideoPolicy(b: MediaBudget, s: GovernorSeverity): IncomingVideoPolicy {
    // Severity 4: no incoming video
    if (s >= 4) {
      return {
        maxVisibleVideos: 0,
        activeSpeakerOnly: true,
        maxIncomingWidth: 0,
        maxIncomingFps: 0,
      };
    }

    // Severity 3: active speaker only
    if (s >= 3) {
      return {
        maxVisibleVideos: 1,
        activeSpeakerOnly: true,
        maxIncomingWidth: 360,
        maxIncomingFps: 10,
      };
    }

    // Severity 2: limited
    if (s >= 2) {
      return {
        maxVisibleVideos: Math.min(b.maxVideoParticipants, 2),
        activeSpeakerOnly: false,
        maxIncomingWidth: 480,
        maxIncomingFps: 15,
      };
    }

    // Severity 1: slightly limited
    if (s >= 1) {
      return {
        maxVisibleVideos: Math.min(b.maxVideoParticipants, 4),
        activeSpeakerOnly: false,
        maxIncomingWidth: 640,
        maxIncomingFps: 24,
      };
    }

    // Nominal
    return {
      maxVisibleVideos: b.maxVideoParticipants,
      activeSpeakerOnly: false,
      maxIncomingWidth: b.maxVideoWidth,
      maxIncomingFps: b.maxVideoFps,
    };
  }

  private _disabledVideo(reason: string): VideoAllocation {
    return {
      enabled: false,
      maxWidth: 0,
      maxHeight: 0,
      maxFps: 0,
      maxBitrateKbps: 0,
      disableReason: reason,
    };
  }

  private _emit(allocation: MediaAllocation): void {
    for (const cb of this._listeners) {
      try { cb(allocation); } catch {}
    }
  }
}
