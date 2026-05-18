/**
 * HardwareProfiles.ts — Hardware specification profiles & performance mode definitions.
 *
 * Defines:
 *   1. Minimum hardware requirements — the floor below which the app warns the user
 *   2. Recommended hardware requirements — the level at which all features work smoothly
 *   3. Three named performance modes (Eco / Balanced / Performance) with concrete
 *      resource budgets, media caps, and UI behavior modifiers
 *   4. Runtime profile selection based on DeviceCapabilityDetector tier
 *
 * These profiles are consumed by:
 *   - AutoPerformanceManager (orchestrates mode switching)
 *   - ResourceGovernor (enforces CPU/RAM budgets)
 *   - MediaBudgetController (audio-priority allocation)
 *   - RenderOptimizer (animation/DOM complexity caps)
 *   - BackgroundThrottler (idle/background suppression)
 *
 * Does NOT modify any existing service — acts as a pure configuration/data layer.
 */

import type { DeviceTier } from './DeviceCapabilityDetector';

// ── Hardware Specification Profiles ─────────────────────────

export interface HardwareSpec {
  /** CPU: minimum logical core count */
  cpuCores: number;
  /** CPU: approximate minimum clock speed (GHz) */
  cpuClockGHz: number;
  /** RAM: minimum physical memory in GB */
  ramGB: number;
  /** GPU: description of minimum GPU requirement */
  gpu: string;
  /** Storage: free disk space in GB for install + runtime cache */
  storageFreeGB: number;
  /** Network: minimum LAN bandwidth (Mbps) */
  networkMbps: number;
  /** Display: minimum resolution */
  displayResolution: string;
  /** OS: minimum Windows version */
  windowsVersion: string;
  /** Notes for documentation */
  notes: string[];
}

/**
 * Minimum hardware: the app installs and runs but with heavy degradation.
 * Below this, the app shows a warning dialog at startup.
 */
export const MINIMUM_HARDWARE: HardwareSpec = {
  cpuCores: 2,
  cpuClockGHz: 1.6,
  ramGB: 2,
  gpu: 'Any (integrated Intel HD 4000+ or equivalent)',
  storageFreeGB: 0.5,
  networkMbps: 10,
  displayResolution: '1024x768',
  windowsVersion: 'Windows 10 (1903+)',
  notes: [
    'Audio calls only — video may stutter or be disabled',
    'Single call at a time, no group video',
    'Reduced animations and visual effects',
    'Screen share receive only (no broadcasting)',
    'Chat and messaging fully functional',
  ],
};

/**
 * Recommended hardware: all features work smoothly.
 */
export const RECOMMENDED_HARDWARE: HardwareSpec = {
  cpuCores: 4,
  cpuClockGHz: 2.4,
  ramGB: 8,
  gpu: 'Intel UHD 620+ or any discrete GPU',
  storageFreeGB: 1,
  networkMbps: 100,
  displayResolution: '1920x1080',
  windowsVersion: 'Windows 10 (21H2+) or Windows 11',
  notes: [
    'Full HD video calls up to 1080p',
    'Group calls with up to 8 participants',
    'Screen sharing at native resolution',
    'All animations and visual effects enabled',
    'Simultaneous chat, call, and file transfer',
  ],
};

// ── Performance Mode Definitions ────────────────────────────

export type PerformanceMode = 'eco' | 'balanced' | 'performance';

export interface MediaBudget {
  /** Maximum video resolution (width) */
  maxVideoWidth: number;
  /** Maximum video resolution (height) */
  maxVideoHeight: number;
  /** Maximum video framerate */
  maxVideoFps: number;
  /** Maximum video bitrate (kbps) */
  maxVideoBitrateKbps: number;
  /** Maximum audio bitrate (kbps) */
  maxAudioBitrateKbps: number;
  /** Audio sample rate (Hz) */
  audioSampleRate: number;
  /** Maximum participants with video in group calls */
  maxVideoParticipants: number;
  /** Maximum screen share resolution (width) */
  maxScreenShareWidth: number;
  /** Maximum screen share framerate */
  maxScreenShareFps: number;
  /** Whether to prioritize audio over video under pressure */
  audioPriority: boolean;
}

export interface RenderBudget {
  /** Enable CSS animations (transitions, keyframes) */
  enableAnimations: boolean;
  /** Enable backdrop blur / glass effects */
  enableBackdropBlur: boolean;
  /** Enable box shadows */
  enableShadows: boolean;
  /** Maximum concurrent animated elements */
  maxAnimatedElements: number;
  /** Target framerate for UI rendering */
  targetFps: number;
  /** Enable smooth scrolling */
  enableSmoothScroll: boolean;
  /** Maximum visible chat messages in viewport before virtualizing */
  chatVirtualizeThreshold: number;
  /** Enable avatar image loading (vs initials only) */
  enableAvatarImages: boolean;
  /** Enable typing indicator animations */
  enableTypingAnimation: boolean;
  /** Debounce interval for search input (ms) */
  searchDebounceMs: number;
}

export interface ResourceBudget {
  /** Target max CPU usage percentage (renderer process) */
  targetCpuPercent: number;
  /** Target max heap memory (MB) */
  targetHeapMB: number;
  /** Maximum concurrent WebRTC peer connections */
  maxPeerConnections: number;
  /** Maximum open Socket.IO listeners */
  maxSocketListeners: number;
  /** Interval for garbage collection hints (ms), 0 = disabled */
  gcHintIntervalMs: number;
  /** Maximum queued notifications before batching */
  notificationBatchSize: number;
  /** Store update batching window (ms) */
  storeUpdateBatchMs: number;
}

export interface BackgroundBudget {
  /** Throttle interval when app is minimized (ms) */
  minimizedThrottleMs: number;
  /** Throttle interval when app loses focus (ms) */
  unfocusedThrottleMs: number;
  /** Disable video tracks when app is not visible */
  disableVideoWhenHidden: boolean;
  /** Reduce socket polling frequency when idle */
  idleSocketIntervalMs: number;
  /** Pause non-critical timers when minimized */
  pauseNonCriticalTimers: boolean;
  /** Suspend discovery broadcasts when connected */
  suspendDiscoveryWhenConnected: boolean;
}

export interface PerformanceProfile {
  mode: PerformanceMode;
  label: string;
  description: string;
  media: MediaBudget;
  render: RenderBudget;
  resource: ResourceBudget;
  background: BackgroundBudget;
}

// ── Eco Mode — Minimum resource usage ───────────────────────

const ECO_PROFILE: PerformanceProfile = {
  mode: 'eco',
  label: 'Eco',
  description: 'Minimum resource usage. Audio-first, reduced visuals.',
  media: {
    maxVideoWidth: 480,
    maxVideoHeight: 360,
    maxVideoFps: 15,
    maxVideoBitrateKbps: 500,
    maxAudioBitrateKbps: 32,
    audioSampleRate: 16_000,
    maxVideoParticipants: 2,
    maxScreenShareWidth: 1280,
    maxScreenShareFps: 5,
    audioPriority: true,
  },
  render: {
    enableAnimations: false,
    enableBackdropBlur: false,
    enableShadows: false,
    maxAnimatedElements: 0,
    targetFps: 30,
    enableSmoothScroll: false,
    chatVirtualizeThreshold: 30,
    enableAvatarImages: false,
    enableTypingAnimation: false,
    searchDebounceMs: 500,
  },
  resource: {
    targetCpuPercent: 25,
    targetHeapMB: 256,
    maxPeerConnections: 2,
    maxSocketListeners: 20,
    gcHintIntervalMs: 30_000,
    notificationBatchSize: 3,
    storeUpdateBatchMs: 100,
  },
  background: {
    minimizedThrottleMs: 10_000,
    unfocusedThrottleMs: 5_000,
    disableVideoWhenHidden: true,
    idleSocketIntervalMs: 15_000,
    pauseNonCriticalTimers: true,
    suspendDiscoveryWhenConnected: true,
  },
};

// ── Balanced Mode — Default for most users ──────────────────

const BALANCED_PROFILE: PerformanceProfile = {
  mode: 'balanced',
  label: 'Balanced',
  description: 'Good quality with reasonable resource usage.',
  media: {
    maxVideoWidth: 720,
    maxVideoHeight: 480,
    maxVideoFps: 24,
    maxVideoBitrateKbps: 2_000,
    maxAudioBitrateKbps: 48,
    audioSampleRate: 48_000,
    maxVideoParticipants: 5,
    maxScreenShareWidth: 1920,
    maxScreenShareFps: 15,
    audioPriority: true,
  },
  render: {
    enableAnimations: true,
    enableBackdropBlur: false,
    enableShadows: true,
    maxAnimatedElements: 10,
    targetFps: 60,
    enableSmoothScroll: true,
    chatVirtualizeThreshold: 50,
    enableAvatarImages: true,
    enableTypingAnimation: true,
    searchDebounceMs: 300,
  },
  resource: {
    targetCpuPercent: 50,
    targetHeapMB: 512,
    maxPeerConnections: 5,
    maxSocketListeners: 50,
    gcHintIntervalMs: 60_000,
    notificationBatchSize: 5,
    storeUpdateBatchMs: 50,
  },
  background: {
    minimizedThrottleMs: 5_000,
    unfocusedThrottleMs: 2_000,
    disableVideoWhenHidden: true,
    idleSocketIntervalMs: 10_000,
    pauseNonCriticalTimers: false,
    suspendDiscoveryWhenConnected: true,
  },
};

// ── Performance Mode — Full quality, max resources ──────────

const PERFORMANCE_PROFILE: PerformanceProfile = {
  mode: 'performance',
  label: 'Performance',
  description: 'Maximum quality. Uses all available resources.',
  media: {
    maxVideoWidth: 1920,
    maxVideoHeight: 1080,
    maxVideoFps: 30,
    maxVideoBitrateKbps: 8_000,
    maxAudioBitrateKbps: 64,
    audioSampleRate: 48_000,
    maxVideoParticipants: 8,
    maxScreenShareWidth: 1920,
    maxScreenShareFps: 30,
    audioPriority: false,
  },
  render: {
    enableAnimations: true,
    enableBackdropBlur: true,
    enableShadows: true,
    maxAnimatedElements: 50,
    targetFps: 60,
    enableSmoothScroll: true,
    chatVirtualizeThreshold: 100,
    enableAvatarImages: true,
    enableTypingAnimation: true,
    searchDebounceMs: 150,
  },
  resource: {
    targetCpuPercent: 80,
    targetHeapMB: 1024,
    maxPeerConnections: 8,
    maxSocketListeners: 100,
    gcHintIntervalMs: 120_000,
    notificationBatchSize: 10,
    storeUpdateBatchMs: 16,
  },
  background: {
    minimizedThrottleMs: 2_000,
    unfocusedThrottleMs: 1_000,
    disableVideoWhenHidden: false,
    idleSocketIntervalMs: 5_000,
    pauseNonCriticalTimers: false,
    suspendDiscoveryWhenConnected: false,
  },
};

// ── Profile Registry ────────────────────────────────────────

const PROFILES: Record<PerformanceMode, PerformanceProfile> = {
  eco: ECO_PROFILE,
  balanced: BALANCED_PROFILE,
  performance: PERFORMANCE_PROFILE,
};

/**
 * Get a performance profile by mode name.
 */
export function getProfile(mode: PerformanceMode): PerformanceProfile {
  return PROFILES[mode];
}

/**
 * Get all available profiles.
 */
export function getAllProfiles(): PerformanceProfile[] {
  return [ECO_PROFILE, BALANCED_PROFILE, PERFORMANCE_PROFILE];
}

// ── Device Tier → Default Profile Mapping ───────────────────

const TIER_TO_MODE: Record<DeviceTier, PerformanceMode> = {
  minimal: 'eco',
  low: 'eco',
  medium: 'balanced',
  high: 'performance',
};

/**
 * Select the default performance mode based on detected device tier.
 * Users can override this via settings.
 */
export function getDefaultModeForTier(tier: DeviceTier): PerformanceMode {
  return TIER_TO_MODE[tier];
}

/**
 * Check whether the detected hardware meets minimum requirements.
 * Returns an array of specific warnings (empty = all good).
 */
export function checkMinimumRequirements(profile: {
  cpuCores: number;
  memoryGB: number;
  gpuRenderer: string;
}): string[] {
  const warnings: string[] = [];

  if (profile.cpuCores < MINIMUM_HARDWARE.cpuCores) {
    warnings.push(`CPU cores (${profile.cpuCores}) below minimum (${MINIMUM_HARDWARE.cpuCores})`);
  }

  if (profile.memoryGB < MINIMUM_HARDWARE.ramGB) {
    warnings.push(`RAM (${profile.memoryGB}GB) below minimum (${MINIMUM_HARDWARE.ramGB}GB)`);
  }

  // Check for software/virtual GPU renderers
  const gpuLower = profile.gpuRenderer.toLowerCase();
  const SOFTWARE_RENDERERS = ['swiftshader', 'llvmpipe', 'software', 'microsoft basic'];
  if (SOFTWARE_RENDERERS.some(sw => gpuLower.includes(sw))) {
    warnings.push(`Software GPU renderer detected (${profile.gpuRenderer}) — hardware GPU recommended`);
  }

  return warnings;
}

/**
 * Determine if the device should show a hardware warning dialog on startup.
 */
export function shouldShowHardwareWarning(profile: {
  cpuCores: number;
  memoryGB: number;
  gpuRenderer: string;
}): boolean {
  return checkMinimumRequirements(profile).length > 0;
}

// ── Profile Interpolation (for gradual transitions) ─────────

/**
 * Lerp between two profiles' numeric media values. Used during
 * gradual mode transitions to avoid jarring quality jumps.
 *
 * @param from   Source profile
 * @param to     Target profile
 * @param t      Interpolation factor 0-1
 * @returns Interpolated MediaBudget (rounded to integers)
 */
export function interpolateMediaBudget(
  from: MediaBudget,
  to: MediaBudget,
  t: number,
): MediaBudget {
  const clamp = Math.max(0, Math.min(1, t));
  const lerp = (a: number, b: number) => Math.round(a + (b - a) * clamp);

  return {
    maxVideoWidth: lerp(from.maxVideoWidth, to.maxVideoWidth),
    maxVideoHeight: lerp(from.maxVideoHeight, to.maxVideoHeight),
    maxVideoFps: lerp(from.maxVideoFps, to.maxVideoFps),
    maxVideoBitrateKbps: lerp(from.maxVideoBitrateKbps, to.maxVideoBitrateKbps),
    maxAudioBitrateKbps: lerp(from.maxAudioBitrateKbps, to.maxAudioBitrateKbps),
    audioSampleRate: lerp(from.audioSampleRate, to.audioSampleRate),
    maxVideoParticipants: lerp(from.maxVideoParticipants, to.maxVideoParticipants),
    maxScreenShareWidth: lerp(from.maxScreenShareWidth, to.maxScreenShareWidth),
    maxScreenShareFps: lerp(from.maxScreenShareFps, to.maxScreenShareFps),
    audioPriority: clamp < 0.5 ? from.audioPriority : to.audioPriority,
  };
}
