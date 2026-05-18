/**
 * SmartDefaultRules.ts — Phase 16: Master Default Rules Engine
 *
 * The central orchestrator that runs at app startup and on key state
 * changes to ensure every setting has a sensible value without user
 * intervention. Integrates all smart-default subsystems:
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                   Smart Defaults Initialization Flow                  │
 * │                                                                      │
 * │  App Start                                                           │
 * │     │                                                                │
 * │     ▼                                                                │
 * │  ① Detect Hardware Tier (CapacityModel)                              │
 * │     │  → DeviceTier: minimal | low | medium | high                   │
 * │     │  → PCClass:    weak | normal | strong                          │
 * │     ▼                                                                │
 * │  ② Enumerate & Score Devices (DeviceSelectionEngine)                 │
 * │     │  → Best mic, speaker, camera with confidence scores            │
 * │     ▼                                                                │
 * │  ③ Detect Network Conditions (LAN probe)                             │
 * │     │  → NetworkTier: excellent | good | fair | poor                 │
 * │     ▼                                                                │
 * │  ④ Select Quality Profile (MediaQualityAutoSelector)                 │
 * │     │  → Combines HW tier + network tier → quality preset            │
 * │     ▼                                                                │
 * │  ⑤ Apply Smart Defaults (this module)                                │
 * │     │  → Merge: detected → user overrides → safe fallbacks           │
 * │     │  → Write to settings store                                     │
 * │     │  → Emit 'smart_defaults_applied' event                         │
 * │     ▼                                                                │
 * │  ⑥ Ongoing Adaptation                                                │
 * │     • Device hot-swap → re-score, switch if better                   │
 * │     • Network change → re-evaluate quality tier                      │
 * │     • Call start → apply scenario-specific adjustments               │
 * │     • Performance pressure → feed SystemPolicy                       │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * CRITICAL DESIGN PRINCIPLE:
 *   Smart defaults NEVER overwrite explicit user choices.
 *   Every setting has three layers:
 *     1. User-explicit override (highest priority, persisted)
 *     2. Smart-detected value (auto-selected each launch)
 *     3. Safe fallback (hardcoded, always works)
 */

// ── Types ───────────────────────────────────────────────────────

/**
 * Hardware tier as detected by CapacityModel.
 * Re-declared to avoid circular imports.
 */
export type DeviceTier = 'minimal' | 'low' | 'medium' | 'high';
export type PCClass = 'weak' | 'normal' | 'strong';

/**
 * Network quality tier — assessed via LAN RTT probe.
 */
export type NetworkTier = 'excellent' | 'good' | 'fair' | 'poor' | 'unknown';

/**
 * Quality profile label.
 */
export type QualityProfile = 'minimal' | 'low' | 'balanced' | 'high' | 'ultra';

/**
 * Call scenario for context-specific adjustment.
 */
export type CallScenario =
  | 'idle'              // Not in a call
  | 'dm_audio'          // 1-to-1 audio
  | 'dm_video'          // 1-to-1 video
  | 'group_audio_small' // Group audio ≤4
  | 'group_audio_large' // Group audio >4
  | 'group_video_small' // Group video ≤4
  | 'group_video_large' // Group video >4
  | 'screenshare';      // Screen share active

/**
 * Setting origin — tracks where each setting value came from.
 */
export type SettingOrigin = 'user' | 'smart' | 'fallback';

/**
 * A single resolved setting with origin tracking.
 */
export interface ResolvedSetting<T> {
  value: T;
  origin: SettingOrigin;
  confidence: number; // 0-100, how confident the smart detection is
  reason?: string;
}

// ── Environment Snapshot ───────────────────────────────────────

/**
 * Complete environment assessment taken at startup or on demand.
 */
export interface EnvironmentSnapshot {
  /** When this snapshot was taken */
  timestamp: number;

  /** Hardware assessment */
  hardware: {
    tier: DeviceTier;
    pcClass: PCClass;
    cpuCores: number;
    memoryGB: number;
    hasDiscreteGPU: boolean;
    score: number; // 0-100 from CapacityModel
  };

  /** Network assessment */
  network: {
    tier: NetworkTier;
    rttMs: number;
    bandwidthEstimateMbps: number;
    isWifi: boolean;
    isMetered: boolean;
  };

  /** Available devices */
  devices: {
    audioInputCount: number;
    audioOutputCount: number;
    videoInputCount: number;
    hasExternalMic: boolean;
    hasExternalCamera: boolean;
    hasHeadset: boolean;
  };

  /** Power state (Electron only) */
  power: {
    onBattery: boolean;
    batteryLevel: number | null; // 0-1
  };
}

// ── Smart Default Rules ────────────────────────────────────────

/**
 * A single rule that computes a default value based on environment.
 */
export interface SmartDefaultRule<T> {
  /** Setting identifier */
  key: string;
  /** Human-readable description */
  description: string;
  /** Domain for grouping */
  domain: 'device' | 'media' | 'quality' | 'ui' | 'performance' | 'network';
  /** Compute the smart value from environment */
  compute: (env: EnvironmentSnapshot) => ResolvedSetting<T>;
  /** Safe fallback if compute throws */
  fallback: T;
  /** Is this visible in Simple Mode settings? */
  simpleMode: boolean;
  /** i18n key for label */
  labelKey: string;
}

// ── Quality Profile Mapping ────────────────────────────────────

/**
 * Maps (hardware tier × network tier) to a quality profile.
 * Row = hardware tier, Column = network tier.
 *
 *                  excellent   good      fair      poor      unknown
 * high             ultra       high      balanced  low       balanced
 * medium           high        balanced  balanced  low       balanced
 * low              balanced    low       low       minimal   low
 * minimal          low         minimal   minimal   minimal   minimal
 */
export const QUALITY_PROFILE_MATRIX: Record<DeviceTier, Record<NetworkTier, QualityProfile>> = {
  high: {
    excellent: 'ultra',
    good:      'high',
    fair:      'balanced',
    poor:      'low',
    unknown:   'balanced',
  },
  medium: {
    excellent: 'high',
    good:      'balanced',
    fair:      'balanced',
    poor:      'low',
    unknown:   'balanced',
  },
  low: {
    excellent: 'balanced',
    good:      'low',
    fair:      'low',
    poor:      'minimal',
    unknown:   'low',
  },
  minimal: {
    excellent: 'low',
    good:      'minimal',
    fair:      'minimal',
    poor:      'minimal',
    unknown:   'minimal',
  },
};

/**
 * Resolve quality profile from environment.
 */
export function resolveQualityProfile(env: EnvironmentSnapshot): QualityProfile {
  // Battery penalty: drop one tier if on battery with <30%
  let effectiveHwTier = env.hardware.tier;
  if (env.power.onBattery && (env.power.batteryLevel ?? 1) < 0.3) {
    const downgrade: Record<DeviceTier, DeviceTier> = {
      high: 'medium', medium: 'low', low: 'minimal', minimal: 'minimal',
    };
    effectiveHwTier = downgrade[effectiveHwTier];
  }

  return QUALITY_PROFILE_MATRIX[effectiveHwTier][env.network.tier];
}

// ── Scenario Adjustments ───────────────────────────────────────

/**
 * Per-scenario quality multipliers applied on top of the base profile.
 *
 * multiplier < 1.0 = reduce quality
 * multiplier > 1.0 = increase quality (only for 1:1 where resources are free)
 */
export interface ScenarioAdjustment {
  videoBitrateMultiplier: number;
  videoFpsMultiplier: number;
  videoResolutionMultiplier: number;
  audioBitrateMultiplier: number;
  enableScreenShare: boolean;
  enableVideoByDefault: boolean;
  maxParticipantsHint: number;
}

export const SCENARIO_ADJUSTMENTS: Record<CallScenario, ScenarioAdjustment> = {
  idle: {
    videoBitrateMultiplier: 1.0,
    videoFpsMultiplier: 1.0,
    videoResolutionMultiplier: 1.0,
    audioBitrateMultiplier: 1.0,
    enableScreenShare: true,
    enableVideoByDefault: true,
    maxParticipantsHint: 0,
  },
  dm_audio: {
    videoBitrateMultiplier: 0,
    videoFpsMultiplier: 0,
    videoResolutionMultiplier: 0,
    audioBitrateMultiplier: 1.2, // Boost audio quality in 1:1
    enableScreenShare: true,
    enableVideoByDefault: false,
    maxParticipantsHint: 2,
  },
  dm_video: {
    videoBitrateMultiplier: 1.3, // Boost video quality in 1:1 (more bandwidth available)
    videoFpsMultiplier: 1.0,
    videoResolutionMultiplier: 1.2,
    audioBitrateMultiplier: 1.0,
    enableScreenShare: true,
    enableVideoByDefault: true,
    maxParticipantsHint: 2,
  },
  group_audio_small: {
    videoBitrateMultiplier: 0,
    videoFpsMultiplier: 0,
    videoResolutionMultiplier: 0,
    audioBitrateMultiplier: 1.0,
    enableScreenShare: true,
    enableVideoByDefault: false,
    maxParticipantsHint: 4,
  },
  group_audio_large: {
    videoBitrateMultiplier: 0,
    videoFpsMultiplier: 0,
    videoResolutionMultiplier: 0,
    audioBitrateMultiplier: 0.8, // Reduce audio bitrate to save total bandwidth
    enableScreenShare: true,
    enableVideoByDefault: false,
    maxParticipantsHint: 12,
  },
  group_video_small: {
    videoBitrateMultiplier: 0.8,
    videoFpsMultiplier: 1.0,
    videoResolutionMultiplier: 0.8,
    audioBitrateMultiplier: 1.0,
    enableScreenShare: true,
    enableVideoByDefault: true,
    maxParticipantsHint: 4,
  },
  group_video_large: {
    videoBitrateMultiplier: 0.5,
    videoFpsMultiplier: 0.7,
    videoResolutionMultiplier: 0.6,
    audioBitrateMultiplier: 0.9,
    enableScreenShare: false, // Disable screen share in large video groups
    enableVideoByDefault: false,
    maxParticipantsHint: 8,
  },
  screenshare: {
    videoBitrateMultiplier: 0.3, // Drastically reduce video when sharing screen
    videoFpsMultiplier: 0.5,
    videoResolutionMultiplier: 0.5,
    audioBitrateMultiplier: 1.0,
    enableScreenShare: true,
    enableVideoByDefault: false,
    maxParticipantsHint: 8,
  },
};

/**
 * Determine the call scenario from current state.
 */
export function detectCallScenario(params: {
  inCall: boolean;
  channelType: 'dm' | 'group';
  mediaType: 'audio' | 'video';
  participantCount: number;
  isScreenSharing: boolean;
}): CallScenario {
  if (!params.inCall) return 'idle';
  if (params.isScreenSharing) return 'screenshare';

  if (params.channelType === 'dm') {
    return params.mediaType === 'video' ? 'dm_video' : 'dm_audio';
  }

  // Group
  const isLarge = params.participantCount > 4;
  if (params.mediaType === 'video') {
    return isLarge ? 'group_video_large' : 'group_video_small';
  }
  return isLarge ? 'group_audio_large' : 'group_audio_small';
}

// ── Network Tier Detection ─────────────────────────────────────

/**
 * Classify network quality from RTT measurement.
 *
 * Since this is LAN-only:
 *   excellent: RTT < 5ms  (wired or very close AP)
 *   good:      RTT < 20ms (typical WiFi)
 *   fair:      RTT < 50ms (congested WiFi or distant AP)
 *   poor:      RTT ≥ 50ms (severely degraded)
 */
export function classifyNetworkTier(rttMs: number): NetworkTier {
  if (rttMs < 0) return 'unknown';
  if (rttMs < 5) return 'excellent';
  if (rttMs < 20) return 'good';
  if (rttMs < 50) return 'fair';
  return 'poor';
}

/**
 * Probe the server to measure LAN RTT.
 * Returns average of 3 pings in ms, or -1 if unreachable.
 */
export async function probeLanRTT(serverUrl: string, attempts: number = 3): Promise<number> {
  const rtts: number[] = [];

  for (let i = 0; i < attempts; i++) {
    try {
      const start = performance.now();
      const response = await fetch(`${serverUrl}/api/health`, {
        method: 'HEAD',
        cache: 'no-store',
        signal: AbortSignal.timeout(2000),
      });
      if (response.ok) {
        rtts.push(performance.now() - start);
      }
    } catch {
      // Attempt failed — skip
    }

    // Small delay between probes to avoid burst
    if (i < attempts - 1) {
      await new Promise(r => setTimeout(r, 200));
    }
  }

  if (rtts.length === 0) return -1;
  return rtts.reduce((a, b) => a + b, 0) / rtts.length;
}

// ── Environment Detection ──────────────────────────────────────

/**
 * Collect a complete environment snapshot.
 * This is the ENTRY POINT for the smart defaults system.
 */
export async function detectEnvironment(serverUrl: string): Promise<EnvironmentSnapshot> {
  // Hardware
  const cpuCores = navigator.hardwareConcurrency || 2;
  const memoryGB = (navigator as any).deviceMemory || 4;
  const hasDiscreteGPU = await _detectDiscreteGPU();

  // Score: simple heuristic combining cores + memory + GPU
  const hwScore = Math.min(100, Math.round(
    (cpuCores / 16) * 40 +
    (memoryGB / 16) * 30 +
    (hasDiscreteGPU ? 30 : 0),
  ));

  const tier: DeviceTier =
    hwScore >= 75 ? 'high'
      : hwScore >= 50 ? 'medium'
        : hwScore >= 25 ? 'low'
          : 'minimal';

  const pcClass: PCClass =
    tier === 'high' ? 'strong'
      : tier === 'medium' ? 'normal'
        : 'weak';

  // Network
  const rttMs = await probeLanRTT(serverUrl);
  const networkTier = classifyNetworkTier(rttMs);

  const conn = (navigator as any).connection;
  const bandwidthEstimate = conn?.downlink || 100; // LAN default: 100 Mbps
  const isWifi = conn?.type === 'wifi' || !conn?.type; // Assume WiFi if unknown
  const isMetered = conn?.saveData || false;

  // Devices (non-intrusive — no permission request)
  let audioInputCount = 0, audioOutputCount = 0, videoInputCount = 0;
  let hasExternalMic = false, hasExternalCamera = false, hasHeadset = false;
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    for (const d of devices) {
      const label = d.label.toLowerCase();
      if (d.kind === 'audioinput') {
        audioInputCount++;
        if (label.includes('usb') || label.includes('external')) hasExternalMic = true;
        if (label.includes('headset') || label.includes('headphone')) hasHeadset = true;
      }
      if (d.kind === 'audiooutput') audioOutputCount++;
      if (d.kind === 'videoinput') {
        videoInputCount++;
        if (label.includes('usb') || label.includes('external')) hasExternalCamera = true;
      }
    }
  } catch { /* device enumeration failed */ }

  // Power
  let onBattery = false;
  let batteryLevel: number | null = null;
  try {
    const battery = await (navigator as any).getBattery?.();
    if (battery) {
      onBattery = !battery.charging;
      batteryLevel = battery.level;
    }
  } catch { /* no battery API */ }

  return {
    timestamp: Date.now(),
    hardware: { tier, pcClass, cpuCores, memoryGB, hasDiscreteGPU, score: hwScore },
    network: { tier: networkTier, rttMs, bandwidthEstimateMbps: bandwidthEstimate, isWifi, isMetered },
    devices: { audioInputCount, audioOutputCount, videoInputCount, hasExternalMic, hasExternalCamera, hasHeadset },
    power: { onBattery, batteryLevel },
  };
}

/**
 * Detect discrete GPU via WebGL renderer string.
 */
async function _detectDiscreteGPU(): Promise<boolean> {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return false;

    const ext = (gl as WebGLRenderingContext).getExtension('WEBGL_debug_renderer_info');
    if (!ext) return false;

    const renderer = (gl as WebGLRenderingContext).getParameter(ext.UNMASKED_RENDERER_WEBGL).toLowerCase();
    // Discrete GPU indicators
    return renderer.includes('nvidia') ||
      renderer.includes('geforce') ||
      renderer.includes('radeon') ||
      renderer.includes('rx ') ||
      renderer.includes('rtx ') ||
      renderer.includes('gtx ') ||
      renderer.includes('arc ');
  } catch {
    return false;
  }
}

// ── Settings Merge Engine ──────────────────────────────────────

/**
 * The three-layer merge: user overrides → smart detected → fallback.
 *
 * This is the core principle: user choices are SACRED.
 * Smart defaults fill gaps. Fallbacks catch everything else.
 */
export interface SmartDefaultsResult {
  /** The final merged settings to apply */
  settings: Record<string, unknown>;
  /** Origin tracking for each setting */
  origins: Record<string, SettingOrigin>;
  /** The environment snapshot used */
  environment: EnvironmentSnapshot;
  /** Resolved quality profile */
  qualityProfile: QualityProfile;
  /** Confidence score 0-100 (how good the auto-detection was) */
  overallConfidence: number;
  /** Warnings or notes about the detection */
  warnings: string[];
}

/**
 * Check if a user has explicitly set a setting.
 * A setting is "user-explicit" if it exists in localStorage with
 * the `_user_set` flag.
 */
export function isUserExplicitSetting(key: string): boolean {
  try {
    const flags = JSON.parse(localStorage.getItem('commclient_user_overrides') || '{}');
    return flags[key] === true;
  } catch {
    return false;
  }
}

/**
 * Mark a setting as user-explicitly set (called when user changes a setting in UI).
 */
export function markUserExplicit(key: string): void {
  try {
    const flags = JSON.parse(localStorage.getItem('commclient_user_overrides') || '{}');
    flags[key] = true;
    localStorage.setItem('commclient_user_overrides', JSON.stringify(flags));
  } catch { /* storage full or unavailable */ }
}

/**
 * Clear user-explicit flag (called from "Reset to Smart Defaults" UI).
 */
export function clearUserExplicit(key: string): void {
  try {
    const flags = JSON.parse(localStorage.getItem('commclient_user_overrides') || '{}');
    delete flags[key];
    localStorage.setItem('commclient_user_overrides', JSON.stringify(flags));
  } catch { /* no-op */ }
}

/**
 * Clear all user overrides (full reset to smart defaults).
 */
export function clearAllUserOverrides(): void {
  try {
    localStorage.removeItem('commclient_user_overrides');
  } catch { /* no-op */ }
}

// ── Startup Sequence ───────────────────────────────────────────

/**
 * Master startup initialization sequence.
 * Call once at app boot, after basic stores are initialized.
 *
 * Returns the complete smart defaults result which should be
 * applied to the settings store.
 */
export async function initializeSmartDefaults(
  serverUrl: string,
  currentSettings: Record<string, unknown>,
): Promise<SmartDefaultsResult> {
  const warnings: string[] = [];
  let environment: EnvironmentSnapshot;

  // Step 1: Detect environment
  try {
    environment = await detectEnvironment(serverUrl);
  } catch (err) {
    warnings.push(`Environment detection failed: ${(err as Error).message}`);
    environment = _getMinimalEnvironment();
  }

  // Step 2: Resolve quality profile
  const qualityProfile = resolveQualityProfile(environment);

  // Step 3: Compute smart values (will be overridden by DeviceSelectionEngine
  // and MediaQualityAutoSelector in their respective modules)
  const smartSettings: Record<string, unknown> = {
    'quality.profile': qualityProfile,
    'quality.hwTier': environment.hardware.tier,
    'quality.pcClass': environment.hardware.pcClass,
    'quality.networkTier': environment.network.tier,
  };

  // Step 4: Merge with user overrides
  const mergedSettings: Record<string, unknown> = {};
  const origins: Record<string, SettingOrigin> = {};

  // First, apply smart defaults
  for (const [key, value] of Object.entries(smartSettings)) {
    mergedSettings[key] = value;
    origins[key] = 'smart';
  }

  // Then, overlay user-explicit overrides
  for (const [key, value] of Object.entries(currentSettings)) {
    if (isUserExplicitSetting(key)) {
      mergedSettings[key] = value;
      origins[key] = 'user';
    }
  }

  // Confidence: based on how complete the environment detection was
  let confidence = 50; // Base
  if (environment.hardware.score > 0) confidence += 15;
  if (environment.network.rttMs >= 0) confidence += 15;
  if (environment.devices.audioInputCount > 0) confidence += 10;
  if (environment.devices.videoInputCount > 0) confidence += 10;

  return {
    settings: mergedSettings,
    origins,
    environment,
    qualityProfile,
    overallConfidence: Math.min(100, confidence),
    warnings,
  };
}

function _getMinimalEnvironment(): EnvironmentSnapshot {
  return {
    timestamp: Date.now(),
    hardware: { tier: 'low', pcClass: 'weak', cpuCores: 2, memoryGB: 4, hasDiscreteGPU: false, score: 25 },
    network: { tier: 'unknown', rttMs: -1, bandwidthEstimateMbps: 100, isWifi: true, isMetered: false },
    devices: { audioInputCount: 0, audioOutputCount: 0, videoInputCount: 0, hasExternalMic: false, hasExternalCamera: false, hasHeadset: false },
    power: { onBattery: false, batteryLevel: null },
  };
}

// ── Re-evaluation Triggers ─────────────────────────────────────

/**
 * Events that should trigger a re-evaluation of smart defaults.
 */
export const REEVALUATION_TRIGGERS = {
  /** A media device was added or removed */
  DEVICE_CHANGE: 'device_change',
  /** Network quality changed significantly */
  NETWORK_CHANGE: 'network_change',
  /** A call started or ended */
  CALL_STATE_CHANGE: 'call_state_change',
  /** Battery state changed (plugged in / unplugged) */
  POWER_CHANGE: 'power_change',
  /** User explicitly reset to smart defaults */
  USER_RESET: 'user_reset',
  /** App returned from background */
  APP_RESUME: 'app_resume',
} as const;

export type ReevaluationTrigger = typeof REEVALUATION_TRIGGERS[keyof typeof REEVALUATION_TRIGGERS];
