/**
 * GroupCallOptimizer — Bandwidth allocation and tile management for
 * multi-party calls (mesh topology, 2-8 participants).
 *
 * Strategies:
 *   1. Speaker Priority: Active speaker gets 60% bandwidth, others share 40%
 *   2. Voice Activity Detection: Detect speaking via audio level > threshold
 *   3. Tile Visibility: Only request video from visible tiles (viewport)
 *   4. Lazy Video: Late-joiners start audio-only until stable, then upgrade
 *   5. Bandwidth Budget: Total budget / N peers, with speaker bonus
 *
 * Integrates with:
 *   - GracefulDegradationEngine (respects degradation level constraints)
 *   - QualityController (applies per-peer encoding parameters)
 *   - DeviceCapabilityDetector (respects hardware ceiling)
 */

import { type DegradationLevel } from './GracefulDegradationEngine';
import { type DeviceProfile } from './DeviceCapabilityDetector';

// ── Types ──────────────────────────────────────────────

export interface ParticipantBudget {
  peerId: string;
  isSpeaking: boolean;
  isPinned: boolean;
  isVisible: boolean;        // in viewport
  isScreenSharing: boolean;
  videoBitrateKbps: number;
  videoWidth: number;
  videoHeight: number;
  videoFps: number;
  audioEnabled: boolean;
  videoEnabled: boolean;
}

export interface GroupBudgetAllocation {
  totalBudgetKbps: number;
  audioBudgetKbps: number;
  videoBudgetKbps: number;
  participants: ParticipantBudget[];
  activeSpeakerId: string | null;
  degradationLevel: DegradationLevel;
  timestamp: number;
}

export interface SpeakerDetection {
  peerId: string;
  audioLevel: number;     // 0-1
  isSpeaking: boolean;
  lastSpokeAt: number;
}

// ── Constants ──────────────────────────────────────────

const SPEAKER_BANDWIDTH_SHARE = 0.60;     // 60% of video budget to speaker
const NON_SPEAKER_SHARE = 0.40;           // 40% shared among rest
const SPEAKING_THRESHOLD = 0.02;          // Audio level threshold
const SPEAKER_HOLD_MS = 3_000;            // Keep speaker status for 3s after silence
const AUDIO_PER_PEER_KBPS = 48;           // Opus audio budget per peer
const MIN_VIDEO_KBPS = 100;               // Below this, just disable video

// Resolution tiers for non-speakers
const NON_SPEAKER_RESOLUTIONS: Array<{ w: number; h: number; fps: number }> = [
  { w: 320, h: 180, fps: 15 },    // Thumbnail
  { w: 480, h: 270, fps: 15 },    // Small
  { w: 640, h: 360, fps: 24 },    // Medium
  { w: 854, h: 480, fps: 24 },    // Large (only if budget allows)
];

// ── Optimizer Implementation ───────────────────────────

export class GroupCallOptimizer {
  private _participants: Map<string, {
    peerId: string;
    audioLevel: number;
    lastSpokeAt: number;
    isPinned: boolean;
    isVisible: boolean;
    isScreenSharing: boolean;
  }> = new Map();

  private _activeSpeakerId: string | null = null;
  private _degradationLevel: DegradationLevel = 0;
  private _deviceProfile: DeviceProfile | null = null;
  private _totalBudgetKbps: number = 10_000;

  // ── Configuration ─────────────────────────────────────

  setDeviceProfile(profile: DeviceProfile): void {
    this._deviceProfile = profile;
    this._totalBudgetKbps = profile.maxBitrateKbps;
  }

  setDegradationLevel(level: DegradationLevel): void {
    this._degradationLevel = level;
  }

  setTotalBudget(kbps: number): void {
    this._totalBudgetKbps = kbps;
  }

  // ── Participant Management ────────────────────────────

  addParticipant(peerId: string): void {
    if (!this._participants.has(peerId)) {
      this._participants.set(peerId, {
        peerId,
        audioLevel: 0,
        lastSpokeAt: 0,
        isPinned: false,
        isVisible: true,
        isScreenSharing: false,
      });
    }
  }

  removeParticipant(peerId: string): void {
    this._participants.delete(peerId);
    if (this._activeSpeakerId === peerId) {
      this._activeSpeakerId = null;
    }
  }

  setPinned(peerId: string, pinned: boolean): void {
    const p = this._participants.get(peerId);
    if (p) p.isPinned = pinned;
  }

  setVisible(peerId: string, visible: boolean): void {
    const p = this._participants.get(peerId);
    if (p) p.isVisible = visible;
  }

  setScreenSharing(peerId: string, sharing: boolean): void {
    const p = this._participants.get(peerId);
    if (p) p.isScreenSharing = sharing;
  }

  // ── Audio Level Feed ──────────────────────────────────

  /**
   * Feed audio level from a peer's incoming stream.
   * Should be called every ~100ms from an AudioAnalyser node.
   */
  feedAudioLevel(peerId: string, level: number): void {
    const p = this._participants.get(peerId);
    if (!p) return;

    p.audioLevel = level;
    if (level >= SPEAKING_THRESHOLD) {
      p.lastSpokeAt = Date.now();
    }
  }

  // ── Compute Active Speaker ────────────────────────────

  getActiveSpeaker(): string | null {
    return this._activeSpeakerId;
  }

  getSpeakerDetections(): SpeakerDetection[] {
    const now = Date.now();
    return Array.from(this._participants.values()).map(p => ({
      peerId: p.peerId,
      audioLevel: p.audioLevel,
      isSpeaking: p.audioLevel >= SPEAKING_THRESHOLD || (now - p.lastSpokeAt < SPEAKER_HOLD_MS),
      lastSpokeAt: p.lastSpokeAt,
    }));
  }

  private _detectActiveSpeaker(): string | null {
    const now = Date.now();
    let bestPeer: string | null = null;
    let bestLevel = 0;

    for (const p of this._participants.values()) {
      const isSpeaking = p.audioLevel >= SPEAKING_THRESHOLD;
      const wasRecentlySpeaking = (now - p.lastSpokeAt) < SPEAKER_HOLD_MS;

      if ((isSpeaking || wasRecentlySpeaking) && p.audioLevel > bestLevel) {
        bestLevel = p.audioLevel;
        bestPeer = p.peerId;
      }
    }

    // Hysteresis: keep current speaker unless someone else is clearly louder
    if (this._activeSpeakerId && bestPeer !== this._activeSpeakerId) {
      const current = this._participants.get(this._activeSpeakerId);
      if (current) {
        const currentRecentlySpeaking = (now - current.lastSpokeAt) < SPEAKER_HOLD_MS;
        if (currentRecentlySpeaking && bestLevel < current.audioLevel * 1.5) {
          return this._activeSpeakerId; // Keep current speaker
        }
      }
    }

    return bestPeer;
  }

  // ── Compute Budget Allocation ─────────────────────────

  /**
   * Main entry point: compute the bandwidth allocation for all participants.
   * Call this every 2-3 seconds to rebalance.
   */
  computeAllocation(): GroupBudgetAllocation {
    this._activeSpeakerId = this._detectActiveSpeaker();
    const peerCount = this._participants.size;

    if (peerCount === 0) {
      return {
        totalBudgetKbps: this._totalBudgetKbps,
        audioBudgetKbps: 0,
        videoBudgetKbps: 0,
        participants: [],
        activeSpeakerId: null,
        degradationLevel: this._degradationLevel,
        timestamp: Date.now(),
      };
    }

    // Audio budget is fixed: AUDIO_PER_PEER_KBPS per peer
    const audioBudget = peerCount * AUDIO_PER_PEER_KBPS;

    // Video budget = total - audio
    let videoBudget = Math.max(0, this._totalBudgetKbps - audioBudget);

    // Apply degradation limits
    if (this._degradationLevel >= 4) {
      videoBudget = 0; // Audio only
    } else if (this._degradationLevel >= 3) {
      videoBudget = Math.min(videoBudget, 500 * peerCount);
    } else if (this._degradationLevel >= 2) {
      videoBudget = Math.min(videoBudget, 1500 * peerCount);
    }

    // Max visible tiles from degradation hints
    const maxTiles = this._degradationLevel >= 4 ? 0 :
                     this._degradationLevel >= 3 ? 2 :
                     this._degradationLevel >= 2 ? 4 :
                     this._deviceProfile?.maxGroupParticipantsWithVideo || 8;

    // Allocate per-participant
    const participants: ParticipantBudget[] = [];
    const visiblePeers = Array.from(this._participants.values())
      .filter(p => p.isVisible || p.isPinned || p.peerId === this._activeSpeakerId)
      .slice(0, maxTiles);

    const visiblePeerIds = new Set(visiblePeers.map(p => p.peerId));

    for (const p of this._participants.values()) {
      const isSpeaker = p.peerId === this._activeSpeakerId;
      const isVisible = visiblePeerIds.has(p.peerId);
      const shouldHaveVideo = videoBudget > 0 && isVisible && this._degradationLevel < 4;

      let peerVideoBudget = 0;
      let resolution = NON_SPEAKER_RESOLUTIONS[0];

      if (shouldHaveVideo) {
        if (isSpeaker || p.isPinned) {
          // Speaker/pinned gets premium share
          peerVideoBudget = Math.round(videoBudget * SPEAKER_BANDWIDTH_SHARE);
          // Speaker resolution: highest we can afford
          resolution = this._selectResolution(peerVideoBudget);
        } else {
          // Remaining budget shared equally among non-speakers
          const nonSpeakerCount = Math.max(1, visiblePeers.length - (this._activeSpeakerId ? 1 : 0));
          const sharedBudget = videoBudget * NON_SPEAKER_SHARE;
          peerVideoBudget = Math.round(sharedBudget / nonSpeakerCount);
          resolution = this._selectResolution(peerVideoBudget);
        }
      }

      // If budget is too low for meaningful video, disable it
      const videoEnabled = peerVideoBudget >= MIN_VIDEO_KBPS;

      participants.push({
        peerId: p.peerId,
        isSpeaking: isSpeaker,
        isPinned: p.isPinned,
        isVisible,
        isScreenSharing: p.isScreenSharing,
        videoBitrateKbps: videoEnabled ? peerVideoBudget : 0,
        videoWidth: videoEnabled ? resolution.w : 0,
        videoHeight: videoEnabled ? resolution.h : 0,
        videoFps: videoEnabled ? resolution.fps : 0,
        audioEnabled: true,  // Audio always enabled
        videoEnabled,
      });
    }

    return {
      totalBudgetKbps: this._totalBudgetKbps,
      audioBudgetKbps: audioBudget,
      videoBudgetKbps: videoBudget,
      participants,
      activeSpeakerId: this._activeSpeakerId,
      degradationLevel: this._degradationLevel,
      timestamp: Date.now(),
    };
  }

  // ── Resolution Selection ──────────────────────────────

  private _selectResolution(budgetKbps: number): { w: number; h: number; fps: number } {
    // Higher budget → higher resolution
    if (budgetKbps >= 3000) return { w: 1280, h: 720, fps: 30 };
    if (budgetKbps >= 1500) return { w: 854, h: 480, fps: 24 };
    if (budgetKbps >= 800) return { w: 640, h: 360, fps: 24 };
    if (budgetKbps >= 400) return { w: 480, h: 270, fps: 15 };
    return { w: 320, h: 180, fps: 15 };
  }
}
