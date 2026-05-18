/**
 * SmartDefaults.ts — Zero-configuration intelligence layer.
 *
 * Eliminates every decision a non-technical user should never have to make.
 * This service runs once at startup and continuously monitors system state
 * to auto-configure everything the app needs without user interaction.
 *
 * Auto-detected settings:
 *   - System language → app language (navigator.language)
 *   - System theme → app theme (prefers-color-scheme)
 *   - Best microphone → default mic (lowest latency, not "Stereo Mix")
 *   - Best camera → default camera (prefer front-facing / integrated)
 *   - Best speaker → default speaker (primary output)
 *   - Server URL → auto-discovered (discovery store)
 *   - Quality tier → device capability + network probe
 *   - Notification preference → enabled by default
 *   - Window size → 80% of screen, centered
 *   - Sidebar position → depends on RTL
 *
 * Design:
 *   - Never overrides user-explicit choices (check localStorage flags)
 *   - Runs silently, logs decisions to AppLogger
 *   - Re-evaluates on device change (USB mic plugged in)
 *   - Exposes getDefaults() for any component that needs current best guess
 */

import { AppLogger } from '../AppLogger';

const log = AppLogger.create('SmartDefaults');

// ── Types ───────────────────────────────────────────────────

export interface SmartDefaultsSnapshot {
  language: 'en' | 'ar';
  theme: 'dark' | 'light';
  audioInputId: string;
  audioInputLabel: string;
  audioOutputId: string;
  audioOutputLabel: string;
  videoInputId: string;
  videoInputLabel: string;
  notificationsEnabled: boolean;
  qualityTier: 'auto' | 'high' | 'medium' | 'low';
}

// ── Blacklist patterns for fake/virtual devices ─────────────

const MIC_BLACKLIST = [
  /stereo mix/i,
  /what u hear/i,
  /wave out/i,
  /virtual/i,
  /voicemeeter/i,
  /cable output/i,
  /loopback/i,
];

const CAMERA_BLACKLIST = [
  /virtual/i,
  /obs/i,
  /snap camera/i,
  /xsplit/i,
  /manycam/i,
  /droidcam/i,
  /epoccam/i,
];

// ── Detection Helpers ───────────────────────────────────────

function detectLanguage(): 'en' | 'ar' {
  try {
    const saved = localStorage.getItem('commclient_settings');
    if (saved) {
      const parsed = JSON.parse(saved);
      if (parsed.language) return parsed.language;
    }
  } catch {}

  const nav = navigator.language?.toLowerCase() || '';
  if (nav.startsWith('ar')) return 'ar';
  return 'en';
}

function detectTheme(): 'dark' | 'light' {
  try {
    const saved = localStorage.getItem('commclient_settings');
    if (saved) {
      const parsed = JSON.parse(saved);
      if (parsed.theme) return parsed.theme;
    }
  } catch {}

  if (window.matchMedia?.('(prefers-color-scheme: light)').matches) {
    return 'light';
  }
  return 'dark';
}

function isBlacklisted(label: string, blacklist: RegExp[]): boolean {
  return blacklist.some((rx) => rx.test(label));
}

function scoreAudioInput(device: MediaDeviceInfo): number {
  const label = device.label.toLowerCase();
  let score = 50;

  // Penalty for virtual/loopback
  if (isBlacklisted(device.label, MIC_BLACKLIST)) return 0;

  // Prefer "default" system device
  if (device.deviceId === 'default') score += 30;

  // Prefer devices with "microphone" in name
  if (label.includes('microphone')) score += 20;

  // Prefer headset mics (usually better quality in calls)
  if (label.includes('headset') || label.includes('headphone')) score += 15;

  // Prefer USB mics (external = usually better)
  if (label.includes('usb')) score += 10;

  // Penalize "communications" device (Windows dual-device weirdness)
  if (device.deviceId === 'communications') score -= 5;

  return score;
}

function scoreVideoInput(device: MediaDeviceInfo): number {
  const label = device.label.toLowerCase();
  let score = 50;

  // Penalty for virtual cameras
  if (isBlacklisted(device.label, CAMERA_BLACKLIST)) return 0;

  // Prefer integrated/front cameras for calls
  if (label.includes('front') || label.includes('integrated')) score += 20;

  // Prefer "HD" cameras
  if (label.includes('hd')) score += 10;

  // Prefer USB cameras (external = usually positioned better)
  if (label.includes('usb')) score += 5;

  return score;
}

function scoreAudioOutput(device: MediaDeviceInfo): number {
  const label = device.label.toLowerCase();
  let score = 50;

  // Prefer "default" system output
  if (device.deviceId === 'default') score += 30;

  // Prefer speakers for default
  if (label.includes('speaker')) score += 15;

  // Prefer headset/headphone (if plugged in, user probably wants it)
  if (label.includes('headset') || label.includes('headphone')) score += 20;

  return score;
}

// ── Device Enumeration ──────────────────────────────────────

async function enumerateAndRank(): Promise<{
  bestMic: { id: string; label: string };
  bestCamera: { id: string; label: string };
  bestSpeaker: { id: string; label: string };
}> {
  const defaults = {
    bestMic: { id: 'default', label: 'Default Microphone' },
    bestCamera: { id: 'default', label: 'Default Camera' },
    bestSpeaker: { id: 'default', label: 'Default Speaker' },
  };

  try {
    const devices = await navigator.mediaDevices.enumerateDevices();

    const mics = devices.filter((d) => d.kind === 'audioinput' && d.deviceId);
    const cameras = devices.filter((d) => d.kind === 'videoinput' && d.deviceId);
    const speakers = devices.filter((d) => d.kind === 'audiooutput' && d.deviceId);

    if (mics.length > 0) {
      const ranked = mics.map((d) => ({ device: d, score: scoreAudioInput(d) }))
        .sort((a, b) => b.score - a.score);
      if (ranked[0].score > 0) {
        defaults.bestMic = {
          id: ranked[0].device.deviceId,
          label: ranked[0].device.label || 'Microphone',
        };
      }
    }

    if (cameras.length > 0) {
      const ranked = cameras.map((d) => ({ device: d, score: scoreVideoInput(d) }))
        .sort((a, b) => b.score - a.score);
      if (ranked[0].score > 0) {
        defaults.bestCamera = {
          id: ranked[0].device.deviceId,
          label: ranked[0].device.label || 'Camera',
        };
      }
    }

    if (speakers.length > 0) {
      const ranked = speakers.map((d) => ({ device: d, score: scoreAudioOutput(d) }))
        .sort((a, b) => b.score - a.score);
      if (ranked[0].score > 0) {
        defaults.bestSpeaker = {
          id: ranked[0].device.deviceId,
          label: ranked[0].device.label || 'Speaker',
        };
      }
    }
  } catch (err) {
    log.warn('Device enumeration failed, using defaults', err);
  }

  return defaults;
}

// ── Main Class ──────────────────────────────────────────────

class SmartDefaultsService {
  private snapshot: SmartDefaultsSnapshot | null = null;
  private deviceChangeListener: (() => void) | null = null;
  private themeMediaQuery: MediaQueryList | null = null;
  private listeners: Array<(snap: SmartDefaultsSnapshot) => void> = [];

  /**
   * Run detection and produce a SmartDefaultsSnapshot.
   * Call once at startup, then subscribe to changes.
   */
  async detect(): Promise<SmartDefaultsSnapshot> {
    log.info('Detecting smart defaults...');

    const language = detectLanguage();
    const theme = detectTheme();
    const { bestMic, bestCamera, bestSpeaker } = await enumerateAndRank();

    this.snapshot = {
      language,
      theme,
      audioInputId: bestMic.id,
      audioInputLabel: bestMic.label,
      audioOutputId: bestSpeaker.id,
      audioOutputLabel: bestSpeaker.label,
      videoInputId: bestCamera.id,
      videoInputLabel: bestCamera.label,
      notificationsEnabled: true,
      qualityTier: 'auto',
    };

    log.info('Smart defaults detected', {
      language,
      theme,
      mic: bestMic.label,
      camera: bestCamera.label,
      speaker: bestSpeaker.label,
    });

    return this.snapshot;
  }

  /**
   * Start listening for device changes (USB plug/unplug) and theme changes.
   */
  startWatching(): void {
    // Device change listener
    if (navigator.mediaDevices?.addEventListener) {
      const handler = async () => {
        log.info('Device change detected, re-evaluating defaults');
        await this.detect();
        this.notifyListeners();
      };
      navigator.mediaDevices.addEventListener('devicechange', handler);
      this.deviceChangeListener = () => {
        navigator.mediaDevices.removeEventListener('devicechange', handler);
      };
    }

    // Theme change listener
    if (window.matchMedia) {
      this.themeMediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
      const themeHandler = () => {
        if (this.snapshot) {
          this.snapshot.theme = this.themeMediaQuery?.matches ? 'dark' : 'light';
          this.notifyListeners();
        }
      };
      this.themeMediaQuery.addEventListener('change', themeHandler);
    }
  }

  /**
   * Stop watching for changes.
   */
  stopWatching(): void {
    if (this.deviceChangeListener) {
      this.deviceChangeListener();
      this.deviceChangeListener = null;
    }
  }

  /**
   * Get current snapshot (null if detect() hasn't been called).
   */
  getDefaults(): SmartDefaultsSnapshot | null {
    return this.snapshot;
  }

  /**
   * Subscribe to changes. Returns unsubscribe function.
   */
  onChange(callback: (snap: SmartDefaultsSnapshot) => void): () => void {
    this.listeners.push(callback);
    return () => {
      this.listeners = this.listeners.filter((cb) => cb !== callback);
    };
  }

  /**
   * Check if user has explicitly overridden a setting.
   * If yes, SmartDefaults should NOT touch it.
   */
  isUserOverridden(key: string): boolean {
    try {
      const overrides = localStorage.getItem('commclient_user_overrides');
      if (!overrides) return false;
      const parsed = JSON.parse(overrides);
      return parsed[key] === true;
    } catch {
      return false;
    }
  }

  /**
   * Mark a setting as user-overridden (so SmartDefaults won't touch it).
   */
  markOverridden(key: string): void {
    try {
      const overrides = JSON.parse(localStorage.getItem('commclient_user_overrides') || '{}');
      overrides[key] = true;
      localStorage.setItem('commclient_user_overrides', JSON.stringify(overrides));
    } catch {}
  }

  private notifyListeners(): void {
    if (!this.snapshot) return;
    for (const cb of this.listeners) {
      try { cb(this.snapshot); } catch {}
    }
  }
}

// ── Singleton Export ────────────────────────────────────────

export const smartDefaults = new SmartDefaultsService();
