/**
 * SafeFallbackDefaults.ts — Phase 16: Bulletproof Fallback Chain
 *
 * The last line of defense. If smart detection fails, if the user's
 * chosen device is gone, if the network probe times out — these
 * fallbacks guarantee the app starts and works on ANY hardware.
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                      Fallback Priority Chain                          │
 * │                                                                      │
 * │  For every setting:                                                  │
 * │                                                                      │
 * │  ① User-explicit override        highest priority (user chose this)  │
 * │       │ missing?                                                     │
 * │       ▼                                                              │
 * │  ② Smart-detected value           auto-detected from environment     │
 * │       │ detection failed?                                            │
 * │       ▼                                                              │
 * │  ③ Tier-appropriate default       based on hardware tier             │
 * │       │ tier unknown?                                                │
 * │       ▼                                                              │
 * │  ④ Conservative safe fallback     ALWAYS works, lowest common denom  │
 * │                                                                      │
 * │  Rule: app must start and allow audio calls even if EVERY            │
 * │  detection mechanism fails. Audio is life. Video is luxury.          │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * Per-hardware-tier default policy:
 *
 * │ Setting         │ Minimal      │ Low          │ Medium       │ High        │
 * ├─────────────────┼──────────────┼──────────────┼──────────────┼─────────────┤
 * │ Video default   │ OFF          │ ON (480p)    │ ON (720p)    │ ON (1080p)  │
 * │ Audio quality   │ 24kbps/16kHz │ 32kbps/24kHz │ 48kbps/48kHz│ 64kbps/48kHz│
 * │ Screen share    │ DISABLED     │ 1280×5fps    │ 1920×10fps  │ 1920×30fps  │
 * │ Noise suppress  │ ON           │ ON           │ ON          │ ON          │
 * │ Echo cancel     │ ON           │ ON           │ ON          │ ON          │
 * │ Animations      │ OFF          │ REDUCED      │ ON          │ ON          │
 * │ Blur effects    │ OFF          │ OFF          │ ON          │ ON          │
 * │ Virtual scroll  │ FORCE        │ FORCE        │ AUTO        │ OFF         │
 * │ Avatar cache    │ 20           │ 50           │ 100         │ 200         │
 * │ Msg cache       │ 100          │ 200          │ 500         │ 1000        │
 * │ Heartbeat       │ 10s          │ 8s           │ 5s          │ 5s          │
 */

import { type DeviceTier, type QualityProfile } from './SmartDefaultRules';

// ── Types ───────────────────────────────────────────────────────

/**
 * The complete set of settings with safe defaults for each tier.
 */
export interface TierDefaults {
  tier: DeviceTier;
  qualityProfile: QualityProfile;

  // Device
  audioInputDeviceId: string;
  audioOutputDeviceId: string;
  videoInputDeviceId: string;

  // Video
  videoOnByDefault: boolean;
  videoWidth: number;
  videoHeight: number;
  videoFps: number;
  videoBitrateKbps: number;

  // Audio
  audioBitrateKbps: number;
  audioSampleRate: number;
  noiseSuppression: boolean;
  echoCancellation: boolean;
  autoGainControl: boolean;

  // Screen Share
  screenShareEnabled: boolean;
  screenShareWidth: number;
  screenShareFps: number;
  screenShareBitrateKbps: number;

  // UI
  animationsEnabled: boolean;
  blurEnabled: boolean;
  shadowsEnabled: boolean;
  transitionsEnabled: boolean;
  typingIndicators: boolean;
  reactionAnimations: boolean;
  forceVirtualScroll: boolean;

  // Background
  heartbeatMs: number;
  discoveryIntervalMs: number;
  presenceIntervalMs: number;
  messageCacheSize: number;
  avatarCacheSize: number;
  thumbnailCacheEnabled: boolean;

  // App
  theme: 'dark' | 'light';
  language: 'en' | 'ar';
  notificationsEnabled: boolean;
  startMinimized: boolean;
  pushToTalk: boolean;
  pushToTalkKey: string;
  autoLockMinutes: number;
}

// ── Tier-Specific Defaults ─────────────────────────────────────

const MINIMAL_DEFAULTS: TierDefaults = {
  tier: 'minimal',
  qualityProfile: 'minimal',

  audioInputDeviceId: 'default',
  audioOutputDeviceId: 'default',
  videoInputDeviceId: '',

  videoOnByDefault: false,
  videoWidth: 320,
  videoHeight: 240,
  videoFps: 15,
  videoBitrateKbps: 200,

  audioBitrateKbps: 24,
  audioSampleRate: 16000,
  noiseSuppression: true,
  echoCancellation: true,
  autoGainControl: true,

  screenShareEnabled: false,
  screenShareWidth: 0,
  screenShareFps: 0,
  screenShareBitrateKbps: 0,

  animationsEnabled: false,
  blurEnabled: false,
  shadowsEnabled: false,
  transitionsEnabled: false,
  typingIndicators: false,
  reactionAnimations: false,
  forceVirtualScroll: true,

  heartbeatMs: 10000,
  discoveryIntervalMs: 30000,
  presenceIntervalMs: 15000,
  messageCacheSize: 100,
  avatarCacheSize: 20,
  thumbnailCacheEnabled: false,

  theme: 'dark',
  language: 'en',
  notificationsEnabled: true,
  startMinimized: false,
  pushToTalk: false,
  pushToTalkKey: 'Space',
  autoLockMinutes: 30,
};

const LOW_DEFAULTS: TierDefaults = {
  ...MINIMAL_DEFAULTS,
  tier: 'low',
  qualityProfile: 'low',

  videoOnByDefault: true,
  videoWidth: 640,
  videoHeight: 480,
  videoFps: 24,
  videoBitrateKbps: 1000,

  audioBitrateKbps: 32,
  audioSampleRate: 24000,

  screenShareEnabled: true,
  screenShareWidth: 1280,
  screenShareFps: 5,
  screenShareBitrateKbps: 500,

  transitionsEnabled: true,
  typingIndicators: true,
  forceVirtualScroll: true,

  heartbeatMs: 8000,
  discoveryIntervalMs: 20000,
  presenceIntervalMs: 10000,
  messageCacheSize: 200,
  avatarCacheSize: 50,
  thumbnailCacheEnabled: true,
};

const MEDIUM_DEFAULTS: TierDefaults = {
  ...LOW_DEFAULTS,
  tier: 'medium',
  qualityProfile: 'balanced',

  videoWidth: 1280,
  videoHeight: 720,
  videoFps: 30,
  videoBitrateKbps: 2500,

  audioBitrateKbps: 48,
  audioSampleRate: 48000,

  screenShareWidth: 1920,
  screenShareFps: 10,
  screenShareBitrateKbps: 1500,

  animationsEnabled: true,
  blurEnabled: true,
  shadowsEnabled: true,
  reactionAnimations: true,
  forceVirtualScroll: false,

  heartbeatMs: 5000,
  discoveryIntervalMs: 15000,
  presenceIntervalMs: 8000,
  messageCacheSize: 500,
  avatarCacheSize: 100,
};

const HIGH_DEFAULTS: TierDefaults = {
  ...MEDIUM_DEFAULTS,
  tier: 'high',
  qualityProfile: 'high',

  videoWidth: 1920,
  videoHeight: 1080,
  videoBitrateKbps: 5000,

  audioBitrateKbps: 64,

  screenShareFps: 30,
  screenShareBitrateKbps: 5000,

  heartbeatMs: 5000,
  messageCacheSize: 1000,
  avatarCacheSize: 200,
};

// ── Lookup ──────────────────────────────────────────────────────

const TIER_DEFAULTS_MAP: Record<DeviceTier, TierDefaults> = {
  minimal: MINIMAL_DEFAULTS,
  low: LOW_DEFAULTS,
  medium: MEDIUM_DEFAULTS,
  high: HIGH_DEFAULTS,
};

/**
 * Get the complete defaults for a given hardware tier.
 */
export function getDefaultsForTier(tier: DeviceTier): TierDefaults {
  return { ...TIER_DEFAULTS_MAP[tier] };
}

/**
 * Get the absolute safest defaults (works on ANY hardware).
 * Used when tier detection itself fails.
 */
export function getSafestDefaults(): TierDefaults {
  return { ...MINIMAL_DEFAULTS };
}

// ── Fallback Resolution ────────────────────────────────────────

/**
 * Resolve a single setting through the 4-layer fallback chain.
 *
 * @param key          Setting key
 * @param userValue    User-explicit override (or undefined)
 * @param smartValue   Smart-detected value (or undefined)
 * @param tier         Hardware tier (or undefined if detection failed)
 * @returns            The resolved value and its origin
 */
export function resolveSetting<K extends keyof TierDefaults>(
  key: K,
  userValue: TierDefaults[K] | undefined,
  smartValue: TierDefaults[K] | undefined,
  tier: DeviceTier | undefined,
): { value: TierDefaults[K]; origin: 'user' | 'smart' | 'tier' | 'fallback' } {
  // Layer 1: User explicit
  if (userValue !== undefined && userValue !== null) {
    return { value: userValue, origin: 'user' };
  }

  // Layer 2: Smart detected
  if (smartValue !== undefined && smartValue !== null) {
    return { value: smartValue, origin: 'smart' };
  }

  // Layer 3: Tier default
  if (tier) {
    const tierDefaults = TIER_DEFAULTS_MAP[tier];
    return { value: tierDefaults[key], origin: 'tier' };
  }

  // Layer 4: Safest fallback
  return { value: MINIMAL_DEFAULTS[key], origin: 'fallback' };
}

/**
 * Resolve all settings at once through the fallback chain.
 */
export function resolveAllSettings(
  userOverrides: Partial<TierDefaults>,
  smartDetected: Partial<TierDefaults>,
  tier: DeviceTier | undefined,
): {
  settings: TierDefaults;
  origins: Record<keyof TierDefaults, 'user' | 'smart' | 'tier' | 'fallback'>;
} {
  const settings = {} as TierDefaults;
  const origins = {} as Record<keyof TierDefaults, 'user' | 'smart' | 'tier' | 'fallback'>;

  const allKeys = Object.keys(MINIMAL_DEFAULTS) as (keyof TierDefaults)[];

  for (const key of allKeys) {
    const result = resolveSetting(
      key,
      userOverrides[key],
      smartDetected[key],
      tier,
    );
    (settings as any)[key] = result.value;
    origins[key] = result.origin;
  }

  return { settings, origins };
}

// ── Audio-First Policy ─────────────────────────────────────────

/**
 * Emergency audio-only fallback constraints.
 *
 * If everything else fails (camera error, bad hardware, network issues),
 * the app MUST still be able to make an audio call with these constraints.
 *
 * These are the absolute minimum: mono audio, low bitrate, noise suppression ON.
 * Works on literally any device manufactured after 2005.
 */
export const EMERGENCY_AUDIO_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    sampleRate: { ideal: 16000 },
    channelCount: 1,
  },
  video: false,
};

/**
 * Emergency video fallback (lowest possible video).
 * Only used if audio-only is not an option and user explicitly wants video.
 */
export const EMERGENCY_VIDEO_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
  video: {
    width: { ideal: 320, max: 480 },
    height: { ideal: 240, max: 360 },
    frameRate: { ideal: 10, max: 15 },
  },
};

// ── getUserMedia Fallback Chain ─────────────────────────────────

/**
 * Try to get media with progressive constraint relaxation.
 *
 * Order:
 *   1. Full constraints (smart-detected quality + chosen device)
 *   2. Relaxed constraints (remove device constraint, keep quality)
 *   3. Minimal constraints (any device, low quality)
 *   4. Emergency audio only (last resort)
 *
 * Returns the stream + which fallback level was used.
 */
export async function getMediaWithFallback(
  primaryConstraints: MediaStreamConstraints,
  wantVideo: boolean = true,
): Promise<{
  stream: MediaStream;
  fallbackLevel: 0 | 1 | 2 | 3;
  fallbackReason?: string;
}> {
  // Level 0: Full constraints
  try {
    const stream = await navigator.mediaDevices.getUserMedia(primaryConstraints);
    return { stream, fallbackLevel: 0 };
  } catch (err0) {
    // Level 1: Relax device constraints (use any device)
    try {
      const relaxed: MediaStreamConstraints = {
        audio: typeof primaryConstraints.audio === 'object'
          ? { ...primaryConstraints.audio, deviceId: undefined }
          : primaryConstraints.audio,
        video: wantVideo && typeof primaryConstraints.video === 'object'
          ? { ...primaryConstraints.video, deviceId: undefined }
          : primaryConstraints.video,
      };
      const stream = await navigator.mediaDevices.getUserMedia(relaxed);
      return { stream, fallbackLevel: 1, fallbackReason: `Primary device unavailable: ${(err0 as Error).message}` };
    } catch (err1) {
      // Level 2: Minimal video constraints
      if (wantVideo) {
        try {
          const stream = await navigator.mediaDevices.getUserMedia(EMERGENCY_VIDEO_CONSTRAINTS);
          return { stream, fallbackLevel: 2, fallbackReason: `Quality constraints too high: ${(err1 as Error).message}` };
        } catch (err2) {
          // Level 3: Audio only (last resort)
          try {
            const stream = await navigator.mediaDevices.getUserMedia(EMERGENCY_AUDIO_CONSTRAINTS);
            return { stream, fallbackLevel: 3, fallbackReason: `Video unavailable: ${(err2 as Error).message}` };
          } catch (err3) {
            throw new Error(`All media fallbacks exhausted: ${(err3 as Error).message}`);
          }
        }
      }

      // No video requested — audio-only fallback
      try {
        const stream = await navigator.mediaDevices.getUserMedia(EMERGENCY_AUDIO_CONSTRAINTS);
        return { stream, fallbackLevel: 2, fallbackReason: `Audio constraints relaxed: ${(err1 as Error).message}` };
      } catch (err3) {
        throw new Error(`All audio fallbacks exhausted: ${(err3 as Error).message}`);
      }
    }
  }
}

// ── Tier Detection Fallback ────────────────────────────────────

/**
 * If the full CapacityModel tier detection fails, use this
 * simple heuristic as a fallback.
 *
 * Only uses navigator.hardwareConcurrency and deviceMemory.
 */
export function detectTierFallback(): DeviceTier {
  const cores = navigator.hardwareConcurrency || 2;
  const memGB = (navigator as any).deviceMemory || 4;

  if (cores >= 8 && memGB >= 16) return 'high';
  if (cores >= 4 && memGB >= 8) return 'medium';
  if (cores >= 2 && memGB >= 4) return 'low';
  return 'minimal';
}

// ── Settings Validation ────────────────────────────────────────

/**
 * Validate and sanitize a settings object.
 * Replaces any invalid/out-of-range values with safe defaults.
 */
export function validateSettings(settings: Partial<TierDefaults>): TierDefaults {
  const safe = getSafestDefaults();
  const result = { ...safe, ...settings };

  // Clamp numeric values to sane ranges
  result.videoWidth = _clamp(result.videoWidth, 160, 1920);
  result.videoHeight = _clamp(result.videoHeight, 120, 1080);
  result.videoFps = _clamp(result.videoFps, 5, 60);
  result.videoBitrateKbps = _clamp(result.videoBitrateKbps, 50, 10000);

  result.audioBitrateKbps = _clamp(result.audioBitrateKbps, 8, 128);
  result.audioSampleRate = _clamp(result.audioSampleRate, 8000, 48000);

  result.screenShareWidth = _clamp(result.screenShareWidth, 0, 3840);
  result.screenShareFps = _clamp(result.screenShareFps, 0, 60);
  result.screenShareBitrateKbps = _clamp(result.screenShareBitrateKbps, 0, 10000);

  result.heartbeatMs = _clamp(result.heartbeatMs, 2000, 30000);
  result.discoveryIntervalMs = _clamp(result.discoveryIntervalMs, 5000, 60000);
  result.presenceIntervalMs = _clamp(result.presenceIntervalMs, 3000, 30000);
  result.messageCacheSize = _clamp(result.messageCacheSize, 50, 5000);
  result.avatarCacheSize = _clamp(result.avatarCacheSize, 10, 500);
  result.autoLockMinutes = _clamp(result.autoLockMinutes, 5, 120);

  // Validate enums
  if (!['dark', 'light'].includes(result.theme)) result.theme = 'dark';
  if (!['en', 'ar'].includes(result.language)) result.language = 'en';
  if (!['minimal', 'low', 'balanced', 'high', 'ultra'].includes(result.qualityProfile)) {
    result.qualityProfile = 'low';
  }

  return result;
}

function _clamp(value: number, min: number, max: number): number {
  if (typeof value !== 'number' || isNaN(value)) return min;
  return Math.max(min, Math.min(max, value));
}
