/**
 * MediaQualityAutoSelector.ts — Phase 16: Adaptive Quality Profiles
 *
 * Translates the resolved QualityProfile + CallScenario into concrete
 * WebRTC constraints (video resolution, FPS, bitrate, audio settings)
 * ready to pass to getUserMedia() and RTCPeerConnection.
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                  Quality Auto-Selection Pipeline                      │
 * │                                                                      │
 * │  EnvironmentSnapshot                                                 │
 * │       │                                                              │
 * │       ▼                                                              │
 * │  resolveQualityProfile()                                             │
 * │       │  → QualityProfile: minimal|low|balanced|high|ultra           │
 * │       ▼                                                              │
 * │  getBaseConstraints(profile)                                         │
 * │       │  → base video/audio/screenshare constraints                  │
 * │       ▼                                                              │
 * │  applyScenarioAdjustment(base, scenario)                             │
 * │       │  → scenario-tuned constraints (group size, screenshare, etc) │
 * │       ▼                                                              │
 * │  clampToCapacity(adjusted, hwTier)                                   │
 * │       │  → never exceeds CapacityModel resource ceilings             │
 * │       ▼                                                              │
 * │  Final MediaConstraints                                              │
 * │       │                                                              │
 * │       ├──► getUserMedia(constraints.audio, constraints.video)         │
 * │       ├──► RTCRtpSender.setParameters(bitrate)                       │
 * │       └──► UI: show quality indicator                                │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import {
  type QualityProfile,
  type CallScenario,
  type DeviceTier,
  type EnvironmentSnapshot,
  SCENARIO_ADJUSTMENTS,
} from './SmartDefaultRules';

// ── Constraint Types ───────────────────────────────────────────

export interface VideoConstraints {
  width: number;
  height: number;
  frameRate: number;
  bitrateKbps: number;
  /** Ideal values (getUserMedia ideal) */
  idealWidth: number;
  idealHeight: number;
  idealFrameRate: number;
}

export interface AudioConstraints {
  bitrateKbps: number;
  sampleRate: number;
  noiseSuppression: boolean;
  echoCancellation: boolean;
  autoGainControl: boolean;
  /** Opus DTX (discontinuous transmission) — saves bandwidth in silence */
  dtx: boolean;
  /** Opus FEC (forward error correction) — helps on lossy networks */
  fec: boolean;
}

export interface ScreenShareConstraints {
  enabled: boolean;
  maxWidth: number;
  maxFps: number;
  bitrateKbps: number;
  /** Content hint: 'detail' for text, 'motion' for video */
  contentHint: 'detail' | 'motion';
  /** Capture system audio */
  systemAudio: boolean;
}

export interface ResolvedMediaConstraints {
  /** Profile that generated these constraints */
  profile: QualityProfile;
  /** Scenario applied */
  scenario: CallScenario;
  /** Self video constraints (for local camera) */
  selfVideo: VideoConstraints;
  /** Expected peer video constraints (what we can receive) */
  peerVideo: VideoConstraints;
  /** Audio constraints */
  audio: AudioConstraints;
  /** Screen share constraints */
  screenShare: ScreenShareConstraints;
  /** Should video be enabled by default? */
  videoOnByDefault: boolean;
  /** Display label for quality level */
  qualityLabelKey: string;
  /** Quality indicator color */
  qualityColor: string;
}

// ── Base Quality Profiles ──────────────────────────────────────

/**
 * Base constraints for each quality profile.
 * These are the STARTING POINT before scenario adjustments.
 */
const BASE_PROFILES: Record<QualityProfile, Omit<ResolvedMediaConstraints, 'profile' | 'scenario' | 'qualityLabelKey' | 'qualityColor'>> = {

  minimal: {
    selfVideo: {
      width: 320, height: 240, frameRate: 15, bitrateKbps: 200,
      idealWidth: 320, idealHeight: 240, idealFrameRate: 15,
    },
    peerVideo: {
      width: 320, height: 240, frameRate: 15, bitrateKbps: 200,
      idealWidth: 320, idealHeight: 240, idealFrameRate: 15,
    },
    audio: {
      bitrateKbps: 24, sampleRate: 16000,
      noiseSuppression: true, echoCancellation: true, autoGainControl: true,
      dtx: true, fec: true,
    },
    screenShare: {
      enabled: false, maxWidth: 0, maxFps: 0, bitrateKbps: 0,
      contentHint: 'detail', systemAudio: false,
    },
    videoOnByDefault: false,
  },

  low: {
    selfVideo: {
      width: 480, height: 360, frameRate: 20, bitrateKbps: 400,
      idealWidth: 640, idealHeight: 480, idealFrameRate: 24,
    },
    peerVideo: {
      width: 480, height: 360, frameRate: 20, bitrateKbps: 500,
      idealWidth: 640, idealHeight: 480, idealFrameRate: 24,
    },
    audio: {
      bitrateKbps: 32, sampleRate: 24000,
      noiseSuppression: true, echoCancellation: true, autoGainControl: true,
      dtx: true, fec: true,
    },
    screenShare: {
      enabled: true, maxWidth: 1280, maxFps: 5, bitrateKbps: 500,
      contentHint: 'detail', systemAudio: false,
    },
    videoOnByDefault: true,
  },

  balanced: {
    selfVideo: {
      width: 640, height: 480, frameRate: 24, bitrateKbps: 1000,
      idealWidth: 1280, idealHeight: 720, idealFrameRate: 30,
    },
    peerVideo: {
      width: 640, height: 480, frameRate: 24, bitrateKbps: 1200,
      idealWidth: 1280, idealHeight: 720, idealFrameRate: 30,
    },
    audio: {
      bitrateKbps: 48, sampleRate: 48000,
      noiseSuppression: true, echoCancellation: true, autoGainControl: true,
      dtx: false, fec: true,
    },
    screenShare: {
      enabled: true, maxWidth: 1920, maxFps: 10, bitrateKbps: 1500,
      contentHint: 'detail', systemAudio: true,
    },
    videoOnByDefault: true,
  },

  high: {
    selfVideo: {
      width: 1280, height: 720, frameRate: 30, bitrateKbps: 2500,
      idealWidth: 1920, idealHeight: 1080, idealFrameRate: 30,
    },
    peerVideo: {
      width: 1280, height: 720, frameRate: 30, bitrateKbps: 3000,
      idealWidth: 1920, idealHeight: 1080, idealFrameRate: 30,
    },
    audio: {
      bitrateKbps: 64, sampleRate: 48000,
      noiseSuppression: true, echoCancellation: true, autoGainControl: true,
      dtx: false, fec: false,
    },
    screenShare: {
      enabled: true, maxWidth: 1920, maxFps: 15, bitrateKbps: 3000,
      contentHint: 'detail', systemAudio: true,
    },
    videoOnByDefault: true,
  },

  ultra: {
    selfVideo: {
      width: 1920, height: 1080, frameRate: 30, bitrateKbps: 5000,
      idealWidth: 1920, idealHeight: 1080, idealFrameRate: 30,
    },
    peerVideo: {
      width: 1920, height: 1080, frameRate: 30, bitrateKbps: 5000,
      idealWidth: 1920, idealHeight: 1080, idealFrameRate: 30,
    },
    audio: {
      bitrateKbps: 64, sampleRate: 48000,
      noiseSuppression: true, echoCancellation: true, autoGainControl: true,
      dtx: false, fec: false,
    },
    screenShare: {
      enabled: true, maxWidth: 1920, maxFps: 30, bitrateKbps: 5000,
      contentHint: 'motion', systemAudio: true,
    },
    videoOnByDefault: true,
  },
};

const PROFILE_META: Record<QualityProfile, { labelKey: string; color: string }> = {
  minimal:  { labelKey: 'smart_defaults.quality.minimal',  color: '#EF4444' },
  low:      { labelKey: 'smart_defaults.quality.low',      color: '#F59E0B' },
  balanced: { labelKey: 'smart_defaults.quality.balanced', color: '#10B981' },
  high:     { labelKey: 'smart_defaults.quality.high',     color: '#3B82F6' },
  ultra:    { labelKey: 'smart_defaults.quality.ultra',    color: '#8B5CF6' },
};

// ── Capacity Ceilings ──────────────────────────────────────────

/**
 * Maximum constraints per hardware tier.
 * These act as hard caps to prevent overwhelming weak hardware.
 */
const TIER_CAPS: Record<DeviceTier, {
  maxVideoWidth: number;
  maxVideoHeight: number;
  maxVideoFps: number;
  maxVideoBitrateKbps: number;
  maxAudioBitrateKbps: number;
  maxScreenShareWidth: number;
  maxScreenShareFps: number;
  maxTotalBitrateKbps: number;
}> = {
  minimal: {
    maxVideoWidth: 480, maxVideoHeight: 360, maxVideoFps: 15,
    maxVideoBitrateKbps: 500, maxAudioBitrateKbps: 32,
    maxScreenShareWidth: 0, maxScreenShareFps: 0,
    maxTotalBitrateKbps: 1000,
  },
  low: {
    maxVideoWidth: 640, maxVideoHeight: 480, maxVideoFps: 24,
    maxVideoBitrateKbps: 1200, maxAudioBitrateKbps: 48,
    maxScreenShareWidth: 1280, maxScreenShareFps: 5,
    maxTotalBitrateKbps: 3000,
  },
  medium: {
    maxVideoWidth: 1280, maxVideoHeight: 720, maxVideoFps: 30,
    maxVideoBitrateKbps: 3000, maxAudioBitrateKbps: 64,
    maxScreenShareWidth: 1920, maxScreenShareFps: 15,
    maxTotalBitrateKbps: 8000,
  },
  high: {
    maxVideoWidth: 1920, maxVideoHeight: 1080, maxVideoFps: 30,
    maxVideoBitrateKbps: 5000, maxAudioBitrateKbps: 64,
    maxScreenShareWidth: 1920, maxScreenShareFps: 30,
    maxTotalBitrateKbps: 20000,
  },
};

// ── Core Functions ──────────────────────────────────────────────

/**
 * Get the complete resolved media constraints for a given quality
 * profile, call scenario, and hardware tier.
 *
 * This is the MAIN ENTRY POINT for components that need constraints.
 *
 * @example
 *   const constraints = resolveMediaConstraints('balanced', 'dm_video', 'medium');
 *   const stream = await navigator.mediaDevices.getUserMedia({
 *     audio: {
 *       sampleRate: constraints.audio.sampleRate,
 *       noiseSuppression: constraints.audio.noiseSuppression,
 *       echoCancellation: constraints.audio.echoCancellation,
 *       autoGainControl: constraints.audio.autoGainControl,
 *     },
 *     video: {
 *       width: { ideal: constraints.selfVideo.idealWidth, max: constraints.selfVideo.width },
 *       height: { ideal: constraints.selfVideo.idealHeight, max: constraints.selfVideo.height },
 *       frameRate: { ideal: constraints.selfVideo.idealFrameRate, max: constraints.selfVideo.frameRate },
 *     },
 *   });
 */
export function resolveMediaConstraints(
  profile: QualityProfile,
  scenario: CallScenario,
  hwTier: DeviceTier,
): ResolvedMediaConstraints {
  // Step 1: Get base profile
  const base = structuredClone(BASE_PROFILES[profile]);

  // Step 2: Apply scenario adjustments
  const adjustment = SCENARIO_ADJUSTMENTS[scenario];
  const adjusted = _applyScenarioMultipliers(base, adjustment);

  // Step 3: Clamp to hardware tier capacity
  const clamped = _clampToTier(adjusted, hwTier);

  // Step 4: Attach metadata
  const meta = PROFILE_META[profile];

  return {
    ...clamped,
    profile,
    scenario,
    qualityLabelKey: meta.labelKey,
    qualityColor: meta.color,
  };
}

/**
 * Get constraints from an EnvironmentSnapshot (convenience wrapper).
 */
export function resolveMediaConstraintsFromEnv(
  env: EnvironmentSnapshot,
  scenario: CallScenario,
  profileOverride?: QualityProfile,
): ResolvedMediaConstraints {
  const { resolveQualityProfile } = require('./SmartDefaultRules');
  const profile = profileOverride || resolveQualityProfile(env);
  return resolveMediaConstraints(profile, scenario, env.hardware.tier);
}

/**
 * Convert ResolvedMediaConstraints to a getUserMedia-compatible object.
 */
export function toGetUserMediaConstraints(
  resolved: ResolvedMediaConstraints,
  options?: {
    audioDeviceId?: string;
    videoDeviceId?: string;
    videoEnabled?: boolean;
  },
): MediaStreamConstraints {
  const audioConstraints: MediaTrackConstraints = {
    sampleRate: { ideal: resolved.audio.sampleRate },
    noiseSuppression: resolved.audio.noiseSuppression,
    echoCancellation: resolved.audio.echoCancellation,
    autoGainControl: resolved.audio.autoGainControl,
  };

  if (options?.audioDeviceId && options.audioDeviceId !== 'default') {
    audioConstraints.deviceId = { exact: options.audioDeviceId };
  }

  const videoEnabled = options?.videoEnabled ?? resolved.videoOnByDefault;

  let videoConstraints: MediaTrackConstraints | boolean = false;
  if (videoEnabled) {
    videoConstraints = {
      width: { ideal: resolved.selfVideo.idealWidth, max: resolved.selfVideo.width },
      height: { ideal: resolved.selfVideo.idealHeight, max: resolved.selfVideo.height },
      frameRate: { ideal: resolved.selfVideo.idealFrameRate, max: resolved.selfVideo.frameRate },
    };

    if (options?.videoDeviceId) {
      (videoConstraints as MediaTrackConstraints).deviceId = { exact: options.videoDeviceId };
    }
  }

  return {
    audio: audioConstraints,
    video: videoConstraints,
  };
}

/**
 * Convert constraints to RTCRtpSendParameters for bitrate control.
 */
export function toBitrateParameters(
  resolved: ResolvedMediaConstraints,
  isScreenShare: boolean = false,
): { maxBitrate: number } {
  if (isScreenShare) {
    return { maxBitrate: resolved.screenShare.bitrateKbps * 1000 };
  }
  return { maxBitrate: resolved.selfVideo.bitrateKbps * 1000 };
}

/**
 * Get screen share display media constraints.
 */
export function toDisplayMediaConstraints(
  resolved: ResolvedMediaConstraints,
): DisplayMediaStreamOptions {
  if (!resolved.screenShare.enabled) {
    throw new Error('Screen share not available at current quality profile');
  }

  return {
    video: {
      width: { max: resolved.screenShare.maxWidth },
      frameRate: { max: resolved.screenShare.maxFps },
    } as MediaTrackConstraints,
    audio: resolved.screenShare.systemAudio,
  };
}

// ── All Profiles Summary ───────────────────────────────────────

/**
 * Get a human-readable summary of all quality profiles.
 * Useful for the settings UI profile selector.
 */
export function getProfileSummaries(): Array<{
  profile: QualityProfile;
  labelKey: string;
  color: string;
  videoLabel: string;
  audioLabel: string;
  screenShareLabel: string;
}> {
  return (['minimal', 'low', 'balanced', 'high', 'ultra'] as QualityProfile[]).map(p => {
    const base = BASE_PROFILES[p];
    const meta = PROFILE_META[p];
    return {
      profile: p,
      labelKey: meta.labelKey,
      color: meta.color,
      videoLabel: base.selfVideo.width === 0 ? 'Audio only'
        : `${base.selfVideo.width}×${base.selfVideo.height} @ ${base.selfVideo.frameRate}fps`,
      audioLabel: `${base.audio.bitrateKbps}kbps / ${base.audio.sampleRate / 1000}kHz`,
      screenShareLabel: base.screenShare.enabled
        ? `${base.screenShare.maxWidth}px @ ${base.screenShare.maxFps}fps`
        : 'Disabled',
    };
  });
}

// ── Private Helpers ─────────────────────────────────────────────

function _applyScenarioMultipliers(
  base: Omit<ResolvedMediaConstraints, 'profile' | 'scenario' | 'qualityLabelKey' | 'qualityColor'>,
  adj: typeof SCENARIO_ADJUSTMENTS[CallScenario],
): typeof base {
  const result = structuredClone(base);

  // Video
  result.selfVideo.bitrateKbps = Math.round(result.selfVideo.bitrateKbps * adj.videoBitrateMultiplier);
  result.selfVideo.frameRate = Math.round(result.selfVideo.frameRate * adj.videoFpsMultiplier);
  result.selfVideo.width = Math.round(result.selfVideo.width * adj.videoResolutionMultiplier);
  result.selfVideo.height = Math.round(result.selfVideo.height * adj.videoResolutionMultiplier);
  result.selfVideo.idealWidth = Math.round(result.selfVideo.idealWidth * adj.videoResolutionMultiplier);
  result.selfVideo.idealHeight = Math.round(result.selfVideo.idealHeight * adj.videoResolutionMultiplier);
  result.selfVideo.idealFrameRate = Math.round(result.selfVideo.idealFrameRate * adj.videoFpsMultiplier);

  result.peerVideo.bitrateKbps = Math.round(result.peerVideo.bitrateKbps * adj.videoBitrateMultiplier);
  result.peerVideo.frameRate = Math.round(result.peerVideo.frameRate * adj.videoFpsMultiplier);
  result.peerVideo.width = Math.round(result.peerVideo.width * adj.videoResolutionMultiplier);
  result.peerVideo.height = Math.round(result.peerVideo.height * adj.videoResolutionMultiplier);

  // Audio
  result.audio.bitrateKbps = Math.round(result.audio.bitrateKbps * adj.audioBitrateMultiplier);

  // Screen share
  if (!adj.enableScreenShare) {
    result.screenShare.enabled = false;
  }

  // Video on by default
  result.videoOnByDefault = adj.enableVideoByDefault && result.videoOnByDefault;

  return result;
}

function _clampToTier(
  constraints: Omit<ResolvedMediaConstraints, 'profile' | 'scenario' | 'qualityLabelKey' | 'qualityColor'>,
  tier: DeviceTier,
): typeof constraints {
  const caps = TIER_CAPS[tier];
  const result = structuredClone(constraints);

  // Clamp self video
  result.selfVideo.width = Math.min(result.selfVideo.width, caps.maxVideoWidth);
  result.selfVideo.height = Math.min(result.selfVideo.height, caps.maxVideoHeight);
  result.selfVideo.frameRate = Math.min(result.selfVideo.frameRate, caps.maxVideoFps);
  result.selfVideo.bitrateKbps = Math.min(result.selfVideo.bitrateKbps, caps.maxVideoBitrateKbps);
  result.selfVideo.idealWidth = Math.min(result.selfVideo.idealWidth, caps.maxVideoWidth);
  result.selfVideo.idealHeight = Math.min(result.selfVideo.idealHeight, caps.maxVideoHeight);
  result.selfVideo.idealFrameRate = Math.min(result.selfVideo.idealFrameRate, caps.maxVideoFps);

  // Clamp peer video
  result.peerVideo.width = Math.min(result.peerVideo.width, caps.maxVideoWidth);
  result.peerVideo.height = Math.min(result.peerVideo.height, caps.maxVideoHeight);
  result.peerVideo.frameRate = Math.min(result.peerVideo.frameRate, caps.maxVideoFps);
  result.peerVideo.bitrateKbps = Math.min(result.peerVideo.bitrateKbps, caps.maxVideoBitrateKbps);

  // Clamp audio
  result.audio.bitrateKbps = Math.min(result.audio.bitrateKbps, caps.maxAudioBitrateKbps);

  // Clamp screen share
  if (caps.maxScreenShareWidth === 0) {
    result.screenShare.enabled = false;
  } else {
    result.screenShare.maxWidth = Math.min(result.screenShare.maxWidth, caps.maxScreenShareWidth);
    result.screenShare.maxFps = Math.min(result.screenShare.maxFps, caps.maxScreenShareFps);
  }

  return result;
}
