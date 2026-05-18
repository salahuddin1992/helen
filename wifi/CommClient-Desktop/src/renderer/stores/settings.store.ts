import { create } from 'zustand';
import type { AppSettings } from '../types';

const SETTINGS_KEY = 'commclient_settings';

/**
 * Theme application — supports 'dark', 'light', and 'system'. The 'system'
 * mode follows the OS prefers-color-scheme media query in real time so the
 * app re-themes when the user toggles dark mode at the OS level. The
 * listener is registered once and lives for the lifetime of the renderer.
 */
let _systemThemeListenerInstalled = false;
let _currentThemePref: AppSettings['theme'] = 'dark';

function applyTheme(theme: AppSettings['theme']): void {
  _currentThemePref = theme;
  const isDark = theme === 'system'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : theme === 'dark';
  document.documentElement.classList.toggle('dark', isDark);

  if (!_systemThemeListenerInstalled) {
    _systemThemeListenerInstalled = true;
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => {
      if (_currentThemePref !== 'system') return;
      document.documentElement.classList.toggle('dark', mql.matches);
    };
    // Both .addEventListener and .addListener exist; use the modern path.
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', handler);
    } else {
      // Older Safari fallback.
       
      (mql as any).addListener?.(handler);
    }
  }
}

const defaults: AppSettings = {
  serverUrl: 'http://127.0.0.1:3000',
  theme: 'dark',
  language: 'en',
  dndUntil: null,
  channelMutes: {},
  audioInputDevice: 'default',
  audioOutputDevice: 'default',
  videoInputDevice: '',
  notifications: true,
  startMinimized: false,
  pushToTalk: false,
  pushToTalkKey: 'Space',
  // Safe defaults that work well on typical LAN calls and laptop webcams.
  videoResolution: '720p',
  videoFrameRate: 30,
  mirrorCamera: true,
  customVideoWidth: 1280,
  customVideoHeight: 720,
  customVideoFrameRate: 30,
  useCustomFrameRate: false,
  audioSampleRate: 48000,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  microphoneGain: 100,
  speakerVolume: 100,
  autoMaxQuality: false,
};

interface SettingsState {
  settings: AppSettings;
  load: () => void;
  update: (partial: Partial<AppSettings>) => void;
  reset: () => void;
}

/**
 * Persistence layers:
 *   1. Main-process settings.json (via IPC)  — survives uninstall/reinstall.
 *   2. localStorage cache (sync read on boot) — keeps reads instant.
 *
 * Reads go to localStorage first then fire an async refresh from the
 * main process. Writes go to localStorage immediately AND fire an
 * async IPC `settings:save` so the canonical store is always disk-backed.
 *
 * Falls back gracefully when running in a plain browser (web PWA) where
 * the IPC bridge isn't available — only localStorage is used in that case.
 */
function bridge(): any {
  // The desktop preload exposes window.electronAPI.settings.*.
  // The web PWA shim doesn't (browsers can't write to APPDATA), so
  // we fall back to localStorage-only.

  return (typeof window !== 'undefined' ? (window as any).electronAPI?.settings : null) ?? null;
}

async function persistRemote(updated: AppSettings): Promise<void> {
  const b = bridge();
  if (!b || typeof b.save !== 'function') return;
  try { await b.save(updated); }
  catch (err) {
    console.warn('[settings] save IPC failed (localStorage cache still updated):', err);
  }
}

async function loadRemote(): Promise<Partial<AppSettings> | null> {
  const b = bridge();
  if (!b || typeof b.load !== 'function') return null;
  try { return (await b.load()) as Partial<AppSettings>; }
  catch (err) {
    console.warn('[settings] load IPC failed (using localStorage cache):', err);
    return null;
  }
}

function applySanitization(merged: AppSettings): AppSettings {
  // Virtual device IDs (e.g. `virtual:phone:qt:<udid>`) only exist
  // while the corresponding bridge is live. A stale one persisted
  // from a prior session will make getUserMedia throw and block ALL
  // capture — webcam included. Drop them on load; the bridge
  // re-announces on startup if the phone is still attached.
  if (typeof merged.videoInputDevice === 'string' && merged.videoInputDevice.startsWith('virtual:')) {
    merged.videoInputDevice = '';
  }
  if (typeof merged.audioInputDevice === 'string' && merged.audioInputDevice.startsWith('virtual:')) {
    merged.audioInputDevice = 'default';
  }
  return merged;
}

export const useSettingsStore = create<SettingsState>((set, get) => ({
  settings: defaults,
  load: () => {
    // Phase 1: synchronous read from localStorage so the UI has
    // settings immediately on boot.
    try {
      const stored = localStorage.getItem(SETTINGS_KEY);
      if (stored) {
        const merged = applySanitization({ ...defaults, ...JSON.parse(stored) } as AppSettings);
        applyTheme(merged.theme);
        set({ settings: merged });
      }
    } catch (err) {
      console.warn('[settings] localStorage parse failed; using defaults:', err);
    }
    // Phase 2: async refresh from disk-backed store, merge into local
    // state if it differs (handles reinstall scenarios where
    // localStorage is empty but %APPDATA%/CommClient/settings.json
    // still has the user's preferences).
    void (async () => {
      const remote = await loadRemote();
      if (remote && Object.keys(remote).length > 0) {
        const merged = applySanitization({ ...get().settings, ...remote } as AppSettings);
        applyTheme(merged.theme);
        try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(merged)); }
        catch (err) { console.warn('[settings] cache write failed:', err); }
        set({ settings: merged });
      }
    })();
  },
  update: (partial) => {
    set((s) => {
      const updated = { ...s.settings, ...partial };
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(updated)); }
      catch (err) { console.warn('[settings] cache write failed:', err); }
      void persistRemote(updated);
      if (partial.theme) {
        applyTheme(updated.theme);
      }
      if (partial.language) {
        document.documentElement.dir = updated.language === 'ar' ? 'rtl' : 'ltr';
        document.documentElement.lang = updated.language;
      }
      return { settings: updated };
    });
  },
  reset: () => {
    try { localStorage.removeItem(SETTINGS_KEY); }
    catch (err) { console.warn('[settings] cache clear failed:', err); }
    const b = bridge();
    if (b && typeof b.reset === 'function') {
      void b.reset().catch((err: unknown) => {
        console.warn('[settings] reset IPC failed:', err);
      });
    }
    set({ settings: defaults });
  },
}));
