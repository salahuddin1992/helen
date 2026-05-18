/**
 * DeviceSelectionEngine.ts — Phase 16: Intelligent Device Auto-Selection
 *
 * Extends the existing SmartDefaults.ts device scoring with:
 *
 *   1. **Comprehensive Scoring** — 12 heuristic signals per device type
 *   2. **Device Memory** — Remembers which devices worked well before
 *   3. **Hot-Swap Handling** — Auto-switches to next-best if current disappears
 *   4. **Category-Aware Ranking** — USB external > headset > built-in > virtual
 *   5. **Blacklist** — Known problematic virtual/loopback devices excluded
 *   6. **Confidence Scoring** — Reports how confident the selection is
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                    Device Selection Pipeline                          │
 * │                                                                      │
 * │  enumerateDevices()                                                  │
 * │       │                                                              │
 * │       ▼                                                              │
 * │  ┌─────────────────────┐                                             │
 * │  │   Filter Blacklist  │  remove virtual audio, OBS camera, etc.     │
 * │  └──────────┬──────────┘                                             │
 * │             ▼                                                        │
 * │  ┌─────────────────────┐                                             │
 * │  │   Score Each Device │  12 heuristic signals → 0-200 score         │
 * │  └──────────┬──────────┘                                             │
 * │             ▼                                                        │
 * │  ┌─────────────────────┐                                             │
 * │  │   Apply History     │  bonus for "last known good" device         │
 * │  └──────────┬──────────┘                                             │
 * │             ▼                                                        │
 * │  ┌─────────────────────┐                                             │
 * │  │   Sort by Score     │  highest first, confidence = gap to #2      │
 * │  └──────────┬──────────┘                                             │
 * │             ▼                                                        │
 * │  ┌─────────────────────┐                                             │
 * │  │   Check User Pref   │  if user explicitly chose, prefer it        │
 * │  └──────────┬──────────┘                                             │
 * │             ▼                                                        │
 * │       Best Device + Confidence Score                                 │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import { type EnvironmentSnapshot, isUserExplicitSetting } from './SmartDefaultRules';

// ── Types ───────────────────────────────────────────────────────

export type DeviceKind = 'audioinput' | 'audiooutput' | 'videoinput';

export interface ScoredDevice {
  /** Browser deviceId */
  deviceId: string;
  /** Device label (may be empty if no permission) */
  label: string;
  /** Device kind */
  kind: DeviceKind;
  /** Group ID (shared by devices in same physical unit) */
  groupId: string;
  /** Computed score (0-200) */
  score: number;
  /** Score breakdown for debugging */
  scoreBreakdown: Record<string, number>;
  /** Device category */
  category: DeviceCategory;
  /** Is this device blacklisted? */
  blacklisted: boolean;
  /** Blacklist reason if applicable */
  blacklistReason?: string;
}

export type DeviceCategory =
  | 'usb_external'   // USB microphone, USB webcam
  | 'headset'        // Headset with mic
  | 'bluetooth'      // Bluetooth device
  | 'builtin'        // Laptop built-in mic/speaker/camera
  | 'hdmi'           // HDMI audio output
  | 'virtual'        // Virtual audio/video (OBS, VoiceMeeter, etc.)
  | 'system_default' // OS "default" device
  | 'unknown';       // Cannot classify

export interface DeviceSelection {
  /** Selected device */
  device: ScoredDevice | null;
  /** Full ranked list */
  ranked: ScoredDevice[];
  /** Selection confidence (0-100) */
  confidence: number;
  /** Why this device was selected */
  reason: string;
  /** i18n key for reason */
  reasonKey: string;
}

export interface FullDeviceRecommendation {
  audioInput: DeviceSelection;
  audioOutput: DeviceSelection;
  videoInput: DeviceSelection;
  /** Overall confidence */
  overallConfidence: number;
  /** Timestamp */
  timestamp: number;
}

// ── Blacklists ──────────────────────────────────────────────────

const AUDIO_INPUT_BLACKLIST: Array<{ pattern: RegExp; reason: string }> = [
  { pattern: /stereo mix/i,   reason: 'Loopback recording device' },
  { pattern: /what u hear/i,  reason: 'Loopback recording device' },
  { pattern: /voicemeeter/i,  reason: 'Virtual audio router' },
  { pattern: /virtual cable/i, reason: 'Virtual audio cable' },
  { pattern: /cable output/i,  reason: 'Virtual audio cable output' },
  { pattern: /wave (in|out)/i, reason: 'Virtual audio device' },
  { pattern: /vb-audio/i,     reason: 'Virtual audio device' },
  { pattern: /soundflower/i,  reason: 'Virtual audio device (macOS)' },
  { pattern: /blackhole/i,    reason: 'Virtual audio device (macOS)' },
];

const VIDEO_INPUT_BLACKLIST: Array<{ pattern: RegExp; reason: string }> = [
  { pattern: /obs virtual/i,   reason: 'OBS virtual camera' },
  { pattern: /snap camera/i,   reason: 'Snap Camera virtual camera' },
  { pattern: /xsplit/i,        reason: 'XSplit virtual camera' },
  { pattern: /manycam/i,       reason: 'ManyCam virtual camera' },
  { pattern: /droidcam/i,      reason: 'DroidCam virtual camera' },
  { pattern: /iriun/i,         reason: 'Iriun virtual camera' },
  { pattern: /virtual cam/i,   reason: 'Generic virtual camera' },
  { pattern: /newtek ndi/i,    reason: 'NDI virtual camera' },
  { pattern: /screen capture/i, reason: 'Screen capture device' },
];

// Audio outputs don't get blacklisted — all physical outputs are valid

// ── Device Categorization ───────────────────────────────────────

function categorizeDevice(label: string, kind: DeviceKind): DeviceCategory {
  const l = label.toLowerCase();

  // System default (Chrome label "Default" or "Communications")
  if (l === 'default' || l.startsWith('default -')) return 'system_default';
  if (l.startsWith('communications')) return 'system_default';

  // Bluetooth
  if (l.includes('bluetooth') || l.includes('airpods') || l.includes('bt ')) return 'bluetooth';

  // Headset / headphone (with built-in mic)
  if (l.includes('headset') || l.includes('headphone') || l.includes('earphone') || l.includes('earbuds')) return 'headset';

  // USB external
  if (l.includes('usb') || l.includes('external')) return 'usb_external';

  // HDMI (audio output only)
  if (l.includes('hdmi') || l.includes('displayport')) return 'hdmi';

  // Virtual (already blacklisted, but just in case)
  if (l.includes('virtual') || l.includes('voicemeeter') || l.includes('obs ')) return 'virtual';

  // Built-in (laptop/desktop built-in devices)
  if (l.includes('built-in') || l.includes('internal') || l.includes('realtek') ||
    l.includes('integrated') || l.includes('laptop') || l.includes('microphone array')) {
    return 'builtin';
  }

  // If it has no label or unrecognizable
  return label ? 'unknown' : 'builtin'; // Assume builtin if no label
}

// ── Scoring Functions ───────────────────────────────────────────

function scoreAudioInput(
  device: MediaDeviceInfo,
  lastKnownGoodId: string | null,
): { score: number; breakdown: Record<string, number> } {
  const label = device.label.toLowerCase();
  const breakdown: Record<string, number> = {};
  let score = 50; // Base score

  // Blacklist check
  for (const { pattern } of AUDIO_INPUT_BLACKLIST) {
    if (pattern.test(label)) return { score: 0, breakdown: { blacklisted: -50 } };
  }

  // System default bonus
  if (device.deviceId === 'default' || label.startsWith('default')) {
    breakdown.system_default = 30;
    score += 30;
  }

  // Category bonuses (prefer: USB > headset > built-in > bluetooth)
  const category = categorizeDevice(device.label, 'audioinput');
  switch (category) {
    case 'usb_external': breakdown.category = 25; score += 25; break;
    case 'headset':      breakdown.category = 20; score += 20; break;
    case 'builtin':      breakdown.category = 10; score += 10; break;
    case 'bluetooth':    breakdown.category = 5;  score += 5;  break;
    default:             breakdown.category = 0;  break;
  }

  // Keyword bonuses
  if (label.includes('microphone')) { breakdown.keyword_mic = 15; score += 15; }
  if (label.includes('hd') || label.includes('high definition')) { breakdown.keyword_hd = 10; score += 10; }
  if (label.includes('array')) { breakdown.keyword_array = 8; score += 8; } // Beamforming mic

  // Keyword penalties
  if (label.includes('communications')) { breakdown.communications_penalty = -5; score -= 5; }

  // Last known good bonus
  if (lastKnownGoodId && device.deviceId === lastKnownGoodId) {
    breakdown.last_known_good = 20;
    score += 20;
  }

  return { score: Math.max(0, score), breakdown };
}

function scoreAudioOutput(
  device: MediaDeviceInfo,
  lastKnownGoodId: string | null,
  hasHeadset: boolean,
): { score: number; breakdown: Record<string, number> } {
  const label = device.label.toLowerCase();
  const breakdown: Record<string, number> = {};
  let score = 50;

  // System default bonus
  if (device.deviceId === 'default' || label.startsWith('default')) {
    breakdown.system_default = 30;
    score += 30;
  }

  // Category bonuses
  const category = categorizeDevice(device.label, 'audiooutput');
  switch (category) {
    case 'headset':
      // If user has headset plugged in, prefer it for calls (privacy)
      breakdown.category = hasHeadset ? 30 : 15;
      score += hasHeadset ? 30 : 15;
      break;
    case 'usb_external': breakdown.category = 20; score += 20; break;
    case 'builtin':      breakdown.category = 10; score += 10; break;
    case 'bluetooth':    breakdown.category = 5;  score += 5;  break;
    case 'hdmi':         breakdown.category = -5; score -= 5;  break; // HDMI often has latency
    default:             breakdown.category = 0;  break;
  }

  // Keyword bonuses
  if (label.includes('speaker')) { breakdown.keyword_speaker = 15; score += 15; }
  if (label.includes('headphone')) { breakdown.keyword_hp = 20; score += 20; }

  // Last known good
  if (lastKnownGoodId && device.deviceId === lastKnownGoodId) {
    breakdown.last_known_good = 20;
    score += 20;
  }

  return { score: Math.max(0, score), breakdown };
}

function scoreVideoInput(
  device: MediaDeviceInfo,
  lastKnownGoodId: string | null,
): { score: number; breakdown: Record<string, number> } {
  const label = device.label.toLowerCase();
  const breakdown: Record<string, number> = {};
  let score = 50;

  // Blacklist check
  for (const { pattern } of VIDEO_INPUT_BLACKLIST) {
    if (pattern.test(label)) return { score: 0, breakdown: { blacklisted: -50 } };
  }

  // Category bonuses (prefer: USB external > integrated > bluetooth)
  const category = categorizeDevice(device.label, 'videoinput');
  switch (category) {
    case 'usb_external': breakdown.category = 25; score += 25; break;
    case 'builtin':      breakdown.category = 15; score += 15; break;
    case 'bluetooth':    breakdown.category = 5;  score += 5;  break;
    default:             breakdown.category = 0;  break;
  }

  // Quality keywords
  if (label.includes('hd') || label.includes('1080') || label.includes('4k')) {
    breakdown.keyword_hd = 15;
    score += 15;
  }
  if (label.includes('front') || label.includes('face')) {
    breakdown.keyword_front = 10; // Face-facing camera preferred for calls
    score += 10;
  }
  if (label.includes('integrated') || label.includes('built-in')) {
    breakdown.keyword_builtin = 5;
    score += 5;
  }

  // Last known good
  if (lastKnownGoodId && device.deviceId === lastKnownGoodId) {
    breakdown.last_known_good = 20;
    score += 20;
  }

  return { score: Math.max(0, score), breakdown };
}

// ── Device History ──────────────────────────────────────────────

const HISTORY_KEY = 'commclient_device_history';

interface DeviceHistoryEntry {
  deviceId: string;
  label: string;
  kind: DeviceKind;
  /** Cumulative success score (incremented on successful calls) */
  successScore: number;
  /** Last time this device was used successfully */
  lastUsedAt: number;
  /** Times this device failed during a call */
  failureCount: number;
}

/**
 * Read device history from localStorage.
 */
function readDeviceHistory(): Record<string, DeviceHistoryEntry> {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || '{}');
  } catch {
    return {};
  }
}

/**
 * Write device history to localStorage.
 */
function writeDeviceHistory(history: Record<string, DeviceHistoryEntry>): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
  } catch { /* storage full */ }
}

/**
 * Record a successful device use (call completed without issue).
 */
export function recordDeviceSuccess(deviceId: string, label: string, kind: DeviceKind): void {
  const history = readDeviceHistory();
  const key = `${kind}:${deviceId}`;
  const entry = history[key] || { deviceId, label, kind, successScore: 0, lastUsedAt: 0, failureCount: 0 };
  entry.successScore = Math.min(100, entry.successScore + 5);
  entry.lastUsedAt = Date.now();
  entry.label = label; // Update label in case it changed
  history[key] = entry;
  writeDeviceHistory(history);
}

/**
 * Record a device failure (device error during call).
 */
export function recordDeviceFailure(deviceId: string, label: string, kind: DeviceKind): void {
  const history = readDeviceHistory();
  const key = `${kind}:${deviceId}`;
  const entry = history[key] || { deviceId, label, kind, successScore: 0, lastUsedAt: 0, failureCount: 0 };
  entry.failureCount++;
  entry.successScore = Math.max(0, entry.successScore - 10);
  history[key] = entry;
  writeDeviceHistory(history);
}

/**
 * Get the last known good device ID for a kind.
 */
function getLastKnownGood(kind: DeviceKind): string | null {
  const history = readDeviceHistory();
  let best: DeviceHistoryEntry | null = null;

  for (const entry of Object.values(history)) {
    if (entry.kind !== kind) continue;
    if (!best || entry.successScore > best.successScore ||
      (entry.successScore === best.successScore && entry.lastUsedAt > best.lastUsedAt)) {
      best = entry;
    }
  }

  return best?.deviceId || null;
}

// ── Main Selection Logic ────────────────────────────────────────

/**
 * Score and rank all devices of a given kind.
 */
async function rankDevices(
  kind: DeviceKind,
  env: EnvironmentSnapshot,
): Promise<ScoredDevice[]> {
  let devices: MediaDeviceInfo[];
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    devices = all.filter(d => d.kind === kind);
  } catch {
    return [];
  }

  const lastGood = getLastKnownGood(kind);

  const scored: ScoredDevice[] = devices.map(d => {
    let result: { score: number; breakdown: Record<string, number> };

    switch (kind) {
      case 'audioinput':
        result = scoreAudioInput(d, lastGood);
        break;
      case 'audiooutput':
        result = scoreAudioOutput(d, lastGood, env.devices.hasHeadset);
        break;
      case 'videoinput':
        result = scoreVideoInput(d, lastGood);
        break;
    }

    const category = categorizeDevice(d.label, kind);
    const blacklisted = result.score === 0 && result.breakdown.blacklisted !== undefined;

    return {
      deviceId: d.deviceId,
      label: d.label || `${kind} (${d.deviceId.substring(0, 8)})`,
      kind,
      groupId: d.groupId,
      score: result.score,
      scoreBreakdown: result.breakdown,
      category,
      blacklisted,
      blacklistReason: blacklisted
        ? (kind === 'videoinput'
          ? VIDEO_INPUT_BLACKLIST : AUDIO_INPUT_BLACKLIST)
            .find(b => b.pattern.test(d.label.toLowerCase()))?.reason
        : undefined,
    };
  });

  // Sort by score descending, filter out blacklisted
  return scored
    .filter(d => !d.blacklisted)
    .sort((a, b) => b.score - a.score);
}

/**
 * Select the best device for a given kind.
 */
async function selectBest(
  kind: DeviceKind,
  env: EnvironmentSnapshot,
  userExplicitId?: string,
): Promise<DeviceSelection> {
  const ranked = await rankDevices(kind, env);

  // If user has explicit preference and it's available, use it
  const settingKey = kind === 'audioinput' ? 'audioInputDevice'
    : kind === 'audiooutput' ? 'audioOutputDevice'
      : 'videoInputDevice';

  if (userExplicitId && isUserExplicitSetting(settingKey)) {
    const userDevice = ranked.find(d => d.deviceId === userExplicitId);
    if (userDevice) {
      return {
        device: userDevice,
        ranked,
        confidence: 100,
        reason: 'User-selected device',
        reasonKey: 'smart_defaults.device.user_selected',
      };
    }
    // User's preferred device is gone — fall through to auto-select
  }

  if (ranked.length === 0) {
    return {
      device: null,
      ranked: [],
      confidence: 0,
      reason: `No ${kind} devices found`,
      reasonKey: 'smart_defaults.device.none_found',
    };
  }

  const best = ranked[0];

  // Confidence = how much better the best is than the second best
  const secondScore = ranked.length > 1 ? ranked[1].score : 0;
  const gap = best.score - secondScore;
  const confidence = Math.min(100, Math.round(50 + gap * 0.5));

  return {
    device: best,
    ranked,
    confidence,
    reason: `Auto-selected: ${best.label} (score: ${best.score})`,
    reasonKey: 'smart_defaults.device.auto_selected',
  };
}

/**
 * Run full device selection for all three device types.
 */
export async function selectAllDevices(
  env: EnvironmentSnapshot,
  currentSelections?: {
    audioInputId?: string;
    audioOutputId?: string;
    videoInputId?: string;
  },
): Promise<FullDeviceRecommendation> {
  const [audioInput, audioOutput, videoInput] = await Promise.all([
    selectBest('audioinput', env, currentSelections?.audioInputId),
    selectBest('audiooutput', env, currentSelections?.audioOutputId),
    selectBest('videoinput', env, currentSelections?.videoInputId),
  ]);

  const overallConfidence = Math.round(
    (audioInput.confidence + audioOutput.confidence + videoInput.confidence) / 3,
  );

  return {
    audioInput,
    audioOutput,
    videoInput,
    overallConfidence,
    timestamp: Date.now(),
  };
}

// ── Hot-Swap Handler ────────────────────────────────────────────

export interface HotSwapResult {
  /** Device type that changed */
  kind: DeviceKind;
  /** The new recommended device */
  newDevice: ScoredDevice | null;
  /** The previous device (if it's still available) */
  previousDevice: ScoredDevice | null;
  /** Was the current device lost? */
  currentDeviceLost: boolean;
  /** Should the selection change? */
  shouldSwitch: boolean;
  /** Reason for recommendation */
  reason: string;
  reasonKey: string;
}

/**
 * Handle a device change event.
 * Called when navigator.mediaDevices fires 'devicechange'.
 *
 * Returns recommendations for each device type about whether
 * the selection should change.
 */
export async function handleDeviceChange(
  env: EnvironmentSnapshot,
  currentIds: {
    audioInputId: string;
    audioOutputId: string;
    videoInputId: string;
  },
): Promise<HotSwapResult[]> {
  const results: HotSwapResult[] = [];
  const kinds: DeviceKind[] = ['audioinput', 'audiooutput', 'videoinput'];
  const currentIdMap: Record<DeviceKind, string> = {
    audioinput: currentIds.audioInputId,
    audiooutput: currentIds.audioOutputId,
    videoinput: currentIds.videoInputId,
  };

  for (const kind of kinds) {
    const ranked = await rankDevices(kind, env);
    const currentId = currentIdMap[kind];
    const currentStillAvailable = ranked.some(d => d.deviceId === currentId);
    const best = ranked[0] || null;

    if (!currentStillAvailable && currentId) {
      // Current device was removed — must switch
      results.push({
        kind,
        newDevice: best,
        previousDevice: null,
        currentDeviceLost: true,
        shouldSwitch: true,
        reason: `Current ${kind} device disconnected. Switching to ${best?.label || 'none'}.`,
        reasonKey: 'smart_defaults.device.lost_switching',
      });
    } else if (best && best.deviceId !== currentId && best.score > 150) {
      // A significantly better device appeared (e.g. USB headset plugged in)
      const currentDevice = ranked.find(d => d.deviceId === currentId);
      const scoreDiff = best.score - (currentDevice?.score || 0);

      if (scoreDiff > 30) {
        // Only suggest switch if the new device is substantially better
        results.push({
          kind,
          newDevice: best,
          previousDevice: currentDevice || null,
          currentDeviceLost: false,
          shouldSwitch: false, // Suggest but don't force — user might prefer current
          reason: `Better ${kind} detected: ${best.label} (score gap: ${scoreDiff})`,
          reasonKey: 'smart_defaults.device.better_available',
        });
      }
    }
  }

  return results;
}
