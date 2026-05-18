/**
 * CapacityProfileDefaults.ts — Recommended default settings per hardware tier.
 *
 * Consolidates the "recommended experience" for each device tier into a
 * single configuration object. This is the answer to: "What settings
 * should be active when a user at tier X starts CommClient?"
 *
 * Unlike hard limits (which block operations) these defaults define the
 * STARTING quality — what the user gets before any adaptive degradation
 * kicks in.
 *
 * Covers:
 *   - Default call quality (resolution, FPS, bitrate)
 *   - Default screen sharing config
 *   - Default UI feature toggles
 *   - Default background task policy
 *   - Default resource budgets
 *   - User-facing summary for the Settings UI
 *
 * Consumed by:
 *   - AutoPerformanceManager (on startup, applies defaults)
 *   - Settings UI (shows current profile and allows override)
 *   - CallEngine (reads defaults before per-call adaptation)
 *   - ScreenShareEngine (reads defaults for initial capture config)
 */

import type { DeviceTier } from '../performance/DeviceCapabilityDetector';
import type { QualitySpec } from './CallCapacityLimits';
import { tierToPCClass, type PCClass } from './CapacityModel';

// ── Types ───────────────────────────────────────────────────

export interface DefaultVideoConfig {
  /** Default resolution for 1:1 calls (self) */
  oneToOneResolution: { width: number; height: number };
  /** Default FPS for 1:1 video calls */
  oneToOneFps: number;
  /** Default bitrate for 1:1 video calls (kbps) */
  oneToOneBitrateKbps: number;
  /** Default resolution for group calls (self) */
  groupResolution: { width: number; height: number };
  /** Default FPS for group video calls */
  groupFps: number;
  /** Default bitrate for group video calls (kbps) */
  groupBitrateKbps: number;
  /** Whether video is on by default when starting a call */
  videoOnByDefault: boolean;
}

export interface DefaultAudioConfig {
  /** Default audio bitrate (kbps) */
  bitrateKbps: number;
  /** Default sample rate (Hz) */
  sampleRate: number;
  /** Whether noise suppression is enabled */
  noiseSuppression: boolean;
  /** Whether echo cancellation is enabled */
  echoCancellation: boolean;
  /** Whether auto gain control is enabled */
  autoGainControl: boolean;
}

export interface DefaultScreenShareConfig {
  /** Whether screen share is available */
  available: boolean;
  /** Default capture resolution width */
  captureWidth: number;
  /** Default capture FPS */
  captureFps: number;
  /** Default capture bitrate (kbps) */
  captureBitrateKbps: number;
  /** Whether frame differencing is enabled */
  frameDifferencing: boolean;
  /** Whether cursor highlighting is enabled */
  cursorHighlight: boolean;
}

export interface DefaultUIConfig {
  /** Whether CSS animations are enabled */
  animationsEnabled: boolean;
  /** Whether backdrop blur is enabled */
  blurEnabled: boolean;
  /** Whether box shadows are enabled */
  shadowsEnabled: boolean;
  /** Whether transitions are enabled */
  transitionsEnabled: boolean;
  /** Whether typing indicators are shown */
  typingIndicators: boolean;
  /** Whether avatars use full resolution */
  fullResAvatars: boolean;
  /** Whether message reactions animate */
  reactionAnimations: boolean;
  /** Whether virtual scrolling is mandatory */
  forceVirtualScroll: boolean;
  /** Max DOM nodes before warning */
  domNodeWarnThreshold: number;
}

export interface DefaultBackgroundConfig {
  /** Socket heartbeat interval (ms) */
  heartbeatMs: number;
  /** Discovery broadcast interval (ms) */
  discoveryIntervalMs: number;
  /** Message sync batch size */
  syncBatchSize: number;
  /** Contact presence check interval (ms) */
  presenceIntervalMs: number;
  /** Whether to cache thumbnails aggressively */
  aggressiveThumbnailCache: boolean;
  /** LRU cache max entries for messages */
  messageCacheSize: number;
  /** LRU cache max entries for avatars */
  avatarCacheSize: number;
}

export interface DefaultResourceBudget {
  /** Max CPU percentage for CommClient */
  maxCpuPercent: number;
  /** Max heap memory (MB) */
  maxHeapMB: number;
  /** Max total bandwidth for all media (kbps) */
  maxBandwidthKbps: number;
  /** Max peer connections */
  maxPeerConnections: number;
}

export interface CapacityProfileDefault {
  /** Device tier */
  tier: DeviceTier;
  /** PC class */
  pcClass: PCClass;
  /** Profile display name (i18n key) */
  nameKey: string;
  /** Profile description (i18n key) */
  descriptionKey: string;
  /** Default video settings */
  video: DefaultVideoConfig;
  /** Default audio settings */
  audio: DefaultAudioConfig;
  /** Default screen sharing settings */
  screenShare: DefaultScreenShareConfig;
  /** Default UI feature toggles */
  ui: DefaultUIConfig;
  /** Default background task config */
  background: DefaultBackgroundConfig;
  /** Default resource budget */
  resources: DefaultResourceBudget;
  /** Max group audio participants (recommended) */
  recommendedGroupAudio: number;
  /** Max group video participants (recommended) */
  recommendedGroupVideo: number;
  /** Summary i18n keys for settings UI */
  summaryKeys: string[];
}

// ── Per-Tier Defaults ───────────────────────────────────────

const MINIMAL_DEFAULTS: CapacityProfileDefault = {
  tier: 'minimal',
  pcClass: 'weak',
  nameKey: 'capacity.profile_minimal',
  descriptionKey: 'capacity.profile_minimal_desc',
  video: {
    oneToOneResolution: { width: 480, height: 360 },
    oneToOneFps: 15,
    oneToOneBitrateKbps: 400,
    groupResolution: { width: 320, height: 240 },
    groupFps: 15,
    groupBitrateKbps: 200,
    videoOnByDefault: false, // Audio-first on weak devices
  },
  audio: {
    bitrateKbps: 32,
    sampleRate: 16_000,
    noiseSuppression: true,
    echoCancellation: true,
    autoGainControl: true,
  },
  screenShare: {
    available: false,
    captureWidth: 0,
    captureFps: 0,
    captureBitrateKbps: 0,
    frameDifferencing: false,
    cursorHighlight: false,
  },
  ui: {
    animationsEnabled: false,
    blurEnabled: false,
    shadowsEnabled: false,
    transitionsEnabled: false,
    typingIndicators: false,
    fullResAvatars: false,
    reactionAnimations: false,
    forceVirtualScroll: true,
    domNodeWarnThreshold: 3_000,
  },
  background: {
    heartbeatMs: 30_000,
    discoveryIntervalMs: 60_000,
    syncBatchSize: 20,
    presenceIntervalMs: 60_000,
    aggressiveThumbnailCache: false,
    messageCacheSize: 50,
    avatarCacheSize: 30,
  },
  resources: {
    maxCpuPercent: 30,
    maxHeapMB: 256,
    maxBandwidthKbps: 2_000,
    maxPeerConnections: 3,
  },
  recommendedGroupAudio: 4,
  recommendedGroupVideo: 2,
  summaryKeys: [
    'capacity.summary_audio_first',
    'capacity.summary_no_screenshare',
    'capacity.summary_effects_disabled',
    'capacity.summary_small_groups',
  ],
};

const LOW_DEFAULTS: CapacityProfileDefault = {
  tier: 'low',
  pcClass: 'weak',
  nameKey: 'capacity.profile_low',
  descriptionKey: 'capacity.profile_low_desc',
  video: {
    oneToOneResolution: { width: 640, height: 480 },
    oneToOneFps: 24,
    oneToOneBitrateKbps: 1_000,
    groupResolution: { width: 480, height: 360 },
    groupFps: 15,
    groupBitrateKbps: 400,
    videoOnByDefault: true,
  },
  audio: {
    bitrateKbps: 48,
    sampleRate: 24_000,
    noiseSuppression: true,
    echoCancellation: true,
    autoGainControl: true,
  },
  screenShare: {
    available: true,
    captureWidth: 1280,
    captureFps: 5,
    captureBitrateKbps: 1_000,
    frameDifferencing: true,
    cursorHighlight: false,
  },
  ui: {
    animationsEnabled: false,
    blurEnabled: false,
    shadowsEnabled: true,
    transitionsEnabled: true,
    typingIndicators: true,
    fullResAvatars: false,
    reactionAnimations: false,
    forceVirtualScroll: true,
    domNodeWarnThreshold: 5_000,
  },
  background: {
    heartbeatMs: 20_000,
    discoveryIntervalMs: 45_000,
    syncBatchSize: 30,
    presenceIntervalMs: 30_000,
    aggressiveThumbnailCache: false,
    messageCacheSize: 100,
    avatarCacheSize: 80,
  },
  resources: {
    maxCpuPercent: 35,
    maxHeapMB: 300,
    maxBandwidthKbps: 3_000,
    maxPeerConnections: 4,
  },
  recommendedGroupAudio: 6,
  recommendedGroupVideo: 3,
  summaryKeys: [
    'capacity.summary_basic_video',
    'capacity.summary_low_screenshare',
    'capacity.summary_limited_effects',
    'capacity.summary_moderate_groups',
  ],
};

const MEDIUM_DEFAULTS: CapacityProfileDefault = {
  tier: 'medium',
  pcClass: 'normal',
  nameKey: 'capacity.profile_medium',
  descriptionKey: 'capacity.profile_medium_desc',
  video: {
    oneToOneResolution: { width: 1280, height: 720 },
    oneToOneFps: 30,
    oneToOneBitrateKbps: 2_500,
    groupResolution: { width: 640, height: 480 },
    groupFps: 24,
    groupBitrateKbps: 1_000,
    videoOnByDefault: true,
  },
  audio: {
    bitrateKbps: 48,
    sampleRate: 48_000,
    noiseSuppression: true,
    echoCancellation: true,
    autoGainControl: true,
  },
  screenShare: {
    available: true,
    captureWidth: 1920,
    captureFps: 15,
    captureBitrateKbps: 2_500,
    frameDifferencing: true,
    cursorHighlight: true,
  },
  ui: {
    animationsEnabled: true,
    blurEnabled: true,
    shadowsEnabled: true,
    transitionsEnabled: true,
    typingIndicators: true,
    fullResAvatars: true,
    reactionAnimations: true,
    forceVirtualScroll: false, // Only force above threshold
    domNodeWarnThreshold: 8_000,
  },
  background: {
    heartbeatMs: 15_000,
    discoveryIntervalMs: 30_000,
    syncBatchSize: 50,
    presenceIntervalMs: 15_000,
    aggressiveThumbnailCache: true,
    messageCacheSize: 200,
    avatarCacheSize: 200,
  },
  resources: {
    maxCpuPercent: 50,
    maxHeapMB: 512,
    maxBandwidthKbps: 8_000,
    maxPeerConnections: 6,
  },
  recommendedGroupAudio: 10,
  recommendedGroupVideo: 4,
  summaryKeys: [
    'capacity.summary_hd_video',
    'capacity.summary_full_screenshare',
    'capacity.summary_all_effects',
    'capacity.summary_standard_groups',
  ],
};

const HIGH_DEFAULTS: CapacityProfileDefault = {
  tier: 'high',
  pcClass: 'strong',
  nameKey: 'capacity.profile_high',
  descriptionKey: 'capacity.profile_high_desc',
  video: {
    oneToOneResolution: { width: 1920, height: 1080 },
    oneToOneFps: 30,
    oneToOneBitrateKbps: 5_000,
    groupResolution: { width: 1280, height: 720 },
    groupFps: 24,
    groupBitrateKbps: 2_500,
    videoOnByDefault: true,
  },
  audio: {
    bitrateKbps: 96,
    sampleRate: 48_000,
    noiseSuppression: true,
    echoCancellation: true,
    autoGainControl: true,
  },
  screenShare: {
    available: true,
    captureWidth: 1920,
    captureFps: 30,
    captureBitrateKbps: 4_000,
    frameDifferencing: true,
    cursorHighlight: true,
  },
  ui: {
    animationsEnabled: true,
    blurEnabled: true,
    shadowsEnabled: true,
    transitionsEnabled: true,
    typingIndicators: true,
    fullResAvatars: true,
    reactionAnimations: true,
    forceVirtualScroll: false,
    domNodeWarnThreshold: 15_000,
  },
  background: {
    heartbeatMs: 10_000,
    discoveryIntervalMs: 20_000,
    syncBatchSize: 100,
    presenceIntervalMs: 10_000,
    aggressiveThumbnailCache: true,
    messageCacheSize: 500,
    avatarCacheSize: 300,
  },
  resources: {
    maxCpuPercent: 80,
    maxHeapMB: 1_024,
    maxBandwidthKbps: 20_000,
    maxPeerConnections: 10,
  },
  recommendedGroupAudio: 15,
  recommendedGroupVideo: 6,
  summaryKeys: [
    'capacity.summary_fhd_video',
    'capacity.summary_full_screenshare_hd',
    'capacity.summary_all_effects',
    'capacity.summary_large_groups',
  ],
};

// ── Registry ────────────────────────────────────────────────

const TIER_DEFAULTS: Record<DeviceTier, CapacityProfileDefault> = {
  minimal: MINIMAL_DEFAULTS,
  low: LOW_DEFAULTS,
  medium: MEDIUM_DEFAULTS,
  high: HIGH_DEFAULTS,
};

/**
 * Get the recommended defaults for a device tier.
 */
export function getProfileDefaults(tier: DeviceTier): CapacityProfileDefault {
  return TIER_DEFAULTS[tier];
}

/**
 * Get all profile defaults.
 */
export function getAllProfileDefaults(): CapacityProfileDefault[] {
  return [MINIMAL_DEFAULTS, LOW_DEFAULTS, MEDIUM_DEFAULTS, HIGH_DEFAULTS];
}

/**
 * Get the recommended WebRTC constraints for a 1:1 video call.
 */
export function getDefaultVideoConstraints(tier: DeviceTier): MediaTrackConstraints {
  const d = TIER_DEFAULTS[tier].video;
  return {
    width: { ideal: d.oneToOneResolution.width, max: d.oneToOneResolution.width },
    height: { ideal: d.oneToOneResolution.height, max: d.oneToOneResolution.height },
    frameRate: { ideal: d.oneToOneFps, max: d.oneToOneFps },
  };
}

/**
 * Get the recommended WebRTC constraints for a group video call.
 */
export function getDefaultGroupVideoConstraints(tier: DeviceTier): MediaTrackConstraints {
  const d = TIER_DEFAULTS[tier].video;
  return {
    width: { ideal: d.groupResolution.width, max: d.groupResolution.width },
    height: { ideal: d.groupResolution.height, max: d.groupResolution.height },
    frameRate: { ideal: d.groupFps, max: d.groupFps },
  };
}

/**
 * Get the recommended WebRTC audio constraints.
 */
export function getDefaultAudioConstraints(tier: DeviceTier): MediaTrackConstraints {
  const d = TIER_DEFAULTS[tier].audio;
  return {
    sampleRate: { ideal: d.sampleRate },
    echoCancellation: d.echoCancellation,
    noiseSuppression: d.noiseSuppression,
    autoGainControl: d.autoGainControl,
  };
}

/**
 * Get the recommended screen share getDisplayMedia constraints.
 */
export function getDefaultScreenShareConstraints(tier: DeviceTier): DisplayMediaStreamOptions | null {
  const ss = TIER_DEFAULTS[tier].screenShare;
  if (!ss.available) return null;

  return {
    video: {
      width: { ideal: ss.captureWidth, max: ss.captureWidth },
      frameRate: { ideal: ss.captureFps, max: ss.captureFps },
    },
    audio: false,
  };
}

/**
 * Generate CSS custom properties string for the UI config.
 * Intended to be applied to document.documentElement.style.
 */
export function getUICustomProperties(tier: DeviceTier): Record<string, string> {
  const ui = TIER_DEFAULTS[tier].ui;
  return {
    '--cc-animations-enabled': ui.animationsEnabled ? '1' : '0',
    '--cc-blur-enabled': ui.blurEnabled ? '1' : '0',
    '--cc-shadows-enabled': ui.shadowsEnabled ? '1' : '0',
    '--cc-transitions-enabled': ui.transitionsEnabled ? '1' : '0',
    '--cc-reaction-anim': ui.reactionAnimations ? '1' : '0',
    '--cc-dom-warn-threshold': String(ui.domNodeWarnThreshold),
  };
}

/**
 * Get a human-readable comparison table of all tiers.
 * Used by the settings UI to show the user what each tier provides.
 */
export function getProfileComparisonTable(): Array<{
  tier: DeviceTier;
  pcClass: PCClass;
  nameKey: string;
  oneToOne: string;
  groupVideo: string;
  groupAudio: string;
  screenShare: string;
}> {
  return getAllProfileDefaults().map(d => ({
    tier: d.tier,
    pcClass: d.pcClass,
    nameKey: d.nameKey,
    oneToOne: `${d.video.oneToOneResolution.width}x${d.video.oneToOneResolution.height}@${d.video.oneToOneFps}fps`,
    groupVideo: `${d.recommendedGroupVideo} participants @ ${d.video.groupResolution.width}x${d.video.groupResolution.height}`,
    groupAudio: `${d.recommendedGroupAudio} participants`,
    screenShare: d.screenShare.available
      ? `${d.screenShare.captureWidth}px @ ${d.screenShare.captureFps}fps`
      : 'Not available',
  }));
}
