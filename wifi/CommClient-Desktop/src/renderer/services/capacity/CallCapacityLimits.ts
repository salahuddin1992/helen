/**
 * CallCapacityLimits.ts — Concrete limits for every call type per hardware tier.
 *
 * Defines hard limits (absolute maximums) and soft limits (recommended for
 * smooth experience) for:
 *   - 1-to-1 audio calls
 *   - 1-to-1 video calls
 *   - Group audio calls
 *   - Group video calls
 *   - Screen sharing (send and receive)
 *   - Combined scenarios (video call + screen share)
 *
 * Each limit set includes:
 *   - max participants (hard ceiling)
 *   - recommended participants (for consistently smooth experience)
 *   - quality parameters (resolution, FPS, bitrate)
 *   - resource budget allocations
 *   - what degrades first when approaching limits
 *
 * These limits are derived from the CapacityModel CPU cost estimations
 * and validated against the existing GracefulDegradationEngine levels.
 */

import type { DeviceTier } from '../performance/DeviceCapabilityDetector';

// ── Types ───────────────────────────────────────────────────

export interface QualitySpec {
  /** Video resolution width (0 = no video) */
  width: number;
  /** Video resolution height */
  height: number;
  /** Max framerate */
  fps: number;
  /** Video bitrate (kbps) */
  videoBitrateKbps: number;
  /** Audio bitrate (kbps) */
  audioBitrateKbps: number;
  /** Audio sample rate (Hz) */
  audioSampleRate: number;
}

export interface CallLimitSet {
  /** Hard maximum participants (absolute ceiling, enforced) */
  hardMax: number;
  /** Recommended maximum (optimal experience) */
  softMax: number;
  /** Quality for the caller (self) */
  selfQuality: QualitySpec;
  /** Quality for remote peers */
  peerQuality: QualitySpec;
  /** Can the user initiate this call type? */
  allowed: boolean;
  /** Warning message key if approaching limits */
  warningKey: string;
  /** Block message key if at hard limit */
  blockKey: string;
  /** What degrades first (ordered) */
  degradeOrder: string[];
}

export interface ScreenShareLimitSet {
  /** Can send screen share */
  canSend: boolean;
  /** Can receive screen share */
  canReceive: boolean;
  /** Max send resolution width */
  maxSendWidth: number;
  /** Max send FPS */
  maxSendFps: number;
  /** Max send bitrate (kbps) */
  maxSendBitrateKbps: number;
  /** Max receive resolution width */
  maxReceiveWidth: number;
  /** Max receive FPS */
  maxReceiveFps: number;
  /** Can send screen share during a video call */
  canSendDuringVideoCall: boolean;
  /** Quality reduction when sharing during video call */
  videoQualityDuringShare: QualitySpec | null;
}

export interface TierCapacityLimits {
  tier: DeviceTier;
  oneToOneAudio: CallLimitSet;
  oneToOneVideo: CallLimitSet;
  groupAudio: CallLimitSet;
  groupVideo: CallLimitSet;
  screenShare: ScreenShareLimitSet;
}

// ── Quality Presets ─────────────────────────────────────────

const Q_AUDIO_ONLY: QualitySpec = {
  width: 0, height: 0, fps: 0,
  videoBitrateKbps: 0, audioBitrateKbps: 48, audioSampleRate: 48_000,
};

const Q_AUDIO_MINIMAL: QualitySpec = {
  width: 0, height: 0, fps: 0,
  videoBitrateKbps: 0, audioBitrateKbps: 32, audioSampleRate: 16_000,
};

const Q_VIDEO_360p: QualitySpec = {
  width: 480, height: 360, fps: 15,
  videoBitrateKbps: 400, audioBitrateKbps: 32, audioSampleRate: 16_000,
};

const Q_VIDEO_480p: QualitySpec = {
  width: 640, height: 480, fps: 24,
  videoBitrateKbps: 1_000, audioBitrateKbps: 48, audioSampleRate: 48_000,
};

const Q_VIDEO_720p: QualitySpec = {
  width: 1280, height: 720, fps: 30,
  videoBitrateKbps: 2_500, audioBitrateKbps: 64, audioSampleRate: 48_000,
};

const Q_VIDEO_1080p: QualitySpec = {
  width: 1920, height: 1080, fps: 30,
  videoBitrateKbps: 5_000, audioBitrateKbps: 64, audioSampleRate: 48_000,
};

const Q_GROUP_THUMB: QualitySpec = {
  width: 320, height: 240, fps: 15,
  videoBitrateKbps: 200, audioBitrateKbps: 32, audioSampleRate: 16_000,
};

const Q_GROUP_SMALL: QualitySpec = {
  width: 480, height: 360, fps: 20,
  videoBitrateKbps: 500, audioBitrateKbps: 48, audioSampleRate: 48_000,
};

const Q_GROUP_MEDIUM: QualitySpec = {
  width: 640, height: 480, fps: 24,
  videoBitrateKbps: 1_000, audioBitrateKbps: 48, audioSampleRate: 48_000,
};

// ── Per-Tier Limit Definitions ──────────────────────────────

// ── MINIMAL TIER ────────────────────────────────────────────

const MINIMAL_LIMITS: TierCapacityLimits = {
  tier: 'minimal',

  oneToOneAudio: {
    hardMax: 1,
    softMax: 1,
    selfQuality: Q_AUDIO_MINIMAL,
    peerQuality: Q_AUDIO_MINIMAL,
    allowed: true,
    warningKey: 'capacity.audio_only_device',
    blockKey: 'capacity.already_in_call',
    degradeOrder: ['audio_bitrate', 'sample_rate'],
  },

  oneToOneVideo: {
    hardMax: 1,
    softMax: 1,
    selfQuality: Q_VIDEO_360p,
    peerQuality: Q_VIDEO_360p,
    allowed: true,
    warningKey: 'capacity.video_may_lag',
    blockKey: 'capacity.already_in_call',
    degradeOrder: ['video_fps', 'video_resolution', 'disable_video'],
  },

  groupAudio: {
    hardMax: 6,
    softMax: 4,
    selfQuality: Q_AUDIO_MINIMAL,
    peerQuality: Q_AUDIO_MINIMAL,
    allowed: true,
    warningKey: 'capacity.group_audio_limit_near',
    blockKey: 'capacity.group_audio_full',
    degradeOrder: ['audio_bitrate', 'sample_rate'],
  },

  groupVideo: {
    hardMax: 2,
    softMax: 2,
    selfQuality: Q_VIDEO_360p,
    peerQuality: Q_GROUP_THUMB,
    allowed: true,
    warningKey: 'capacity.group_video_limit_near',
    blockKey: 'capacity.group_video_full',
    degradeOrder: ['peer_fps', 'peer_resolution', 'disable_peer_video', 'disable_self_video'],
  },

  screenShare: {
    canSend: false,
    canReceive: true,
    maxSendWidth: 0,
    maxSendFps: 0,
    maxSendBitrateKbps: 0,
    maxReceiveWidth: 1280,
    maxReceiveFps: 5,
    canSendDuringVideoCall: false,
    videoQualityDuringShare: null,
  },
};

// ── LOW TIER ────────────────────────────────────────────────

const LOW_LIMITS: TierCapacityLimits = {
  tier: 'low',

  oneToOneAudio: {
    hardMax: 1,
    softMax: 1,
    selfQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 48, audioSampleRate: 24_000 },
    peerQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 48, audioSampleRate: 24_000 },
    allowed: true,
    warningKey: '',
    blockKey: 'capacity.already_in_call',
    degradeOrder: ['audio_bitrate'],
  },

  oneToOneVideo: {
    hardMax: 1,
    softMax: 1,
    selfQuality: Q_VIDEO_480p,
    peerQuality: Q_VIDEO_480p,
    allowed: true,
    warningKey: '',
    blockKey: 'capacity.already_in_call',
    degradeOrder: ['video_fps', 'video_resolution', 'disable_video'],
  },

  groupAudio: {
    hardMax: 8,
    softMax: 6,
    selfQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 48, audioSampleRate: 24_000 },
    peerQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 48, audioSampleRate: 24_000 },
    allowed: true,
    warningKey: 'capacity.group_audio_limit_near',
    blockKey: 'capacity.group_audio_full',
    degradeOrder: ['audio_bitrate', 'sample_rate'],
  },

  groupVideo: {
    hardMax: 3,
    softMax: 3,
    selfQuality: Q_VIDEO_360p,
    peerQuality: Q_GROUP_THUMB,
    allowed: true,
    warningKey: 'capacity.group_video_limit_near',
    blockKey: 'capacity.group_video_full',
    degradeOrder: ['peer_fps', 'peer_resolution', 'disable_peer_video', 'disable_self_video'],
  },

  screenShare: {
    canSend: true,
    canReceive: true,
    maxSendWidth: 1280,
    maxSendFps: 5,
    maxSendBitrateKbps: 1_000,
    maxReceiveWidth: 1280,
    maxReceiveFps: 10,
    canSendDuringVideoCall: false,
    videoQualityDuringShare: null,
  },
};

// ── MEDIUM TIER ─────────────────────────────────────────────

const MEDIUM_LIMITS: TierCapacityLimits = {
  tier: 'medium',

  oneToOneAudio: {
    hardMax: 1,
    softMax: 1,
    selfQuality: Q_AUDIO_ONLY,
    peerQuality: Q_AUDIO_ONLY,
    allowed: true,
    warningKey: '',
    blockKey: 'capacity.already_in_call',
    degradeOrder: [],
  },

  oneToOneVideo: {
    hardMax: 1,
    softMax: 1,
    selfQuality: Q_VIDEO_720p,
    peerQuality: Q_VIDEO_720p,
    allowed: true,
    warningKey: '',
    blockKey: 'capacity.already_in_call',
    degradeOrder: ['video_fps', 'video_resolution'],
  },

  groupAudio: {
    hardMax: 12,
    softMax: 10,
    selfQuality: Q_AUDIO_ONLY,
    peerQuality: Q_AUDIO_ONLY,
    allowed: true,
    warningKey: 'capacity.group_audio_limit_near',
    blockKey: 'capacity.group_audio_full',
    degradeOrder: ['audio_bitrate'],
  },

  groupVideo: {
    hardMax: 5,
    softMax: 4,
    selfQuality: Q_VIDEO_480p,
    peerQuality: Q_GROUP_SMALL,
    allowed: true,
    warningKey: 'capacity.group_video_limit_near',
    blockKey: 'capacity.group_video_full',
    degradeOrder: ['peer_fps', 'peer_resolution', 'disable_peer_video'],
  },

  screenShare: {
    canSend: true,
    canReceive: true,
    maxSendWidth: 1920,
    maxSendFps: 15,
    maxSendBitrateKbps: 2_500,
    maxReceiveWidth: 1920,
    maxReceiveFps: 15,
    canSendDuringVideoCall: true,
    videoQualityDuringShare: Q_VIDEO_360p,
  },
};

// ── HIGH TIER ───────────────────────────────────────────────

const HIGH_LIMITS: TierCapacityLimits = {
  tier: 'high',

  oneToOneAudio: {
    hardMax: 1,
    softMax: 1,
    selfQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 96, audioSampleRate: 48_000 },
    peerQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 96, audioSampleRate: 48_000 },
    allowed: true,
    warningKey: '',
    blockKey: 'capacity.already_in_call',
    degradeOrder: [],
  },

  oneToOneVideo: {
    hardMax: 1,
    softMax: 1,
    selfQuality: Q_VIDEO_1080p,
    peerQuality: Q_VIDEO_1080p,
    allowed: true,
    warningKey: '',
    blockKey: 'capacity.already_in_call',
    degradeOrder: ['video_fps', 'video_resolution'],
  },

  groupAudio: {
    hardMax: 20,
    softMax: 15,
    selfQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 64, audioSampleRate: 48_000 },
    peerQuality: { ...Q_AUDIO_ONLY, audioBitrateKbps: 64, audioSampleRate: 48_000 },
    allowed: true,
    warningKey: 'capacity.group_audio_limit_near',
    blockKey: 'capacity.group_audio_full',
    degradeOrder: ['audio_bitrate'],
  },

  groupVideo: {
    hardMax: 8,
    softMax: 6,
    selfQuality: Q_VIDEO_720p,
    peerQuality: Q_GROUP_MEDIUM,
    allowed: true,
    warningKey: 'capacity.group_video_limit_near',
    blockKey: 'capacity.group_video_full',
    degradeOrder: ['peer_fps', 'peer_resolution', 'self_resolution'],
  },

  screenShare: {
    canSend: true,
    canReceive: true,
    maxSendWidth: 1920,
    maxSendFps: 30,
    maxSendBitrateKbps: 4_000,
    maxReceiveWidth: 1920,
    maxReceiveFps: 30,
    canSendDuringVideoCall: true,
    videoQualityDuringShare: Q_VIDEO_480p,
  },
};

// ── Registry ────────────────────────────────────────────────

const TIER_LIMITS: Record<DeviceTier, TierCapacityLimits> = {
  minimal: MINIMAL_LIMITS,
  low: LOW_LIMITS,
  medium: MEDIUM_LIMITS,
  high: HIGH_LIMITS,
};

/**
 * Get complete capacity limits for a device tier.
 */
export function getCapacityLimits(tier: DeviceTier): TierCapacityLimits {
  return TIER_LIMITS[tier];
}

/**
 * Check if a specific call type is allowed on this tier.
 */
export function isCallAllowed(
  tier: DeviceTier,
  callType: 'oneToOneAudio' | 'oneToOneVideo' | 'groupAudio' | 'groupVideo',
): boolean {
  return TIER_LIMITS[tier][callType].allowed;
}

/**
 * Check if adding one more participant exceeds the hard limit.
 */
export function canAddParticipant(
  tier: DeviceTier,
  callType: 'groupAudio' | 'groupVideo',
  currentCount: number,
): { allowed: boolean; atSoftLimit: boolean; atHardLimit: boolean; message: string } {
  const limits = TIER_LIMITS[tier][callType];
  const atSoftLimit = currentCount >= limits.softMax;
  const atHardLimit = currentCount >= limits.hardMax;

  if (atHardLimit) {
    return {
      allowed: false,
      atSoftLimit: true,
      atHardLimit: true,
      message: limits.blockKey,
    };
  }

  return {
    allowed: true,
    atSoftLimit,
    atHardLimit: false,
    message: atSoftLimit ? limits.warningKey : '',
  };
}

/**
 * Check if screen sharing is allowed for the current context.
 */
export function canScreenShare(
  tier: DeviceTier,
  direction: 'send' | 'receive',
  isInVideoCall: boolean,
): { allowed: boolean; message: string } {
  const ss = TIER_LIMITS[tier].screenShare;

  if (direction === 'send') {
    if (!ss.canSend) {
      return { allowed: false, message: 'capacity.screenshare_too_weak' };
    }
    if (isInVideoCall && !ss.canSendDuringVideoCall) {
      return { allowed: false, message: 'capacity.screenshare_no_during_video' };
    }
    return { allowed: true, message: '' };
  }

  return {
    allowed: ss.canReceive,
    message: ss.canReceive ? '' : 'capacity.screenshare_receive_disabled',
  };
}

/**
 * Get quality specs for a scenario.
 */
export function getQualityForScenario(
  tier: DeviceTier,
  scenario: {
    callType: 'oneToOneAudio' | 'oneToOneVideo' | 'groupAudio' | 'groupVideo';
    participantCount: number;
    isScreenSharing: boolean;
  },
): { self: QualitySpec; peer: QualitySpec } {
  const limits = TIER_LIMITS[tier][scenario.callType];

  let selfQuality = { ...limits.selfQuality };
  let peerQuality = { ...limits.peerQuality };

  // Reduce quality if screen sharing during video call
  if (scenario.isScreenSharing && scenario.callType === 'groupVideo') {
    const ss = TIER_LIMITS[tier].screenShare;
    if (ss.videoQualityDuringShare) {
      selfQuality = { ...ss.videoQualityDuringShare };
    }
  }

  // Reduce peer quality in large groups
  if (scenario.callType === 'groupVideo' && scenario.participantCount > limits.softMax) {
    // Drop to thumbnail for all peers
    peerQuality = { ...Q_GROUP_THUMB };
  }

  return { self: selfQuality, peer: peerQuality };
}
