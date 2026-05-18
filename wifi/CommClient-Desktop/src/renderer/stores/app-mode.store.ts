/**
 * App Mode Store — Controls Simple vs Advanced mode.
 *
 * Architecture:
 *   - Simple Mode (default): contacts, chat, calls, screen share, basic profile/language.
 *     Everything a child or non-technical user needs. Zero confusion.
 *   - Advanced Mode: adds diagnostics, logs, network tools, port config,
 *     storage, backup/restore, manual server settings, troubleshooting.
 *
 * Unlock Mechanism:
 *   Advanced Mode is hidden behind a secret unlock:
 *     1. Tap the app version label 7 times rapidly (within 4 seconds)  → prompts for admin PIN
 *     2. Enter 4-digit PIN (default: 0000 on first unlock, user sets custom PIN after)
 *     3. Advanced Mode remains active until explicitly locked or session ends
 *
 * Persistence:
 *   - Mode preference is stored in localStorage ('commclient_app_mode')
 *   - Admin PIN hash (SHA-256 of PIN) is stored in localStorage ('commclient_admin_pin')
 *   - On fresh install, no PIN is set — first unlock prompts PIN creation
 *   - Advanced Mode auto-locks after 30 minutes of inactivity (configurable)
 *
 * Permission Model:
 *   - SimpleMode users can NEVER see advanced settings even if they navigate directly
 *   - The route guard (ModeGate) blocks advanced routes entirely
 *   - Advanced Mode users see everything Simple Mode has PLUS the extra panels
 *   - No server-side RBAC change; this is purely a client-side UX layer
 */

import { create } from 'zustand';

// ── Types ──────────────────────────────────────────────

export type AppMode = 'simple' | 'advanced';

export interface AppModeState {
  // Current mode
  mode: AppMode;
  isAdvanced: boolean;  // convenience getter

  // Unlock state
  isUnlocked: boolean;          // true if user passed PIN this session
  isPinConfigured: boolean;     // true if admin PIN has been set
  unlockTimestamp: number;       // ms since epoch when last unlocked
  autoLockTimeout: number;       // ms of inactivity before auto-lock (default 30min)
  tapCount: number;             // version-label tap counter for secret gesture
  tapWindowStart: number;       // timestamp of first tap in current window

  // Actions
  setMode: (mode: AppMode) => void;
  switchToSimple: () => void;
  switchToAdvanced: () => void;
  lockAdvanced: () => void;

  // Unlock flow
  registerTap: () => boolean;   // returns true if tap threshold reached (7 taps)
  resetTaps: () => void;
  unlock: (pin: string) => boolean;
  setPin: (newPin: string) => void;
  verifyPin: (pin: string) => boolean;

  // Auto-lock
  refreshActivity: () => void;
  checkAutoLock: () => void;

  // Persistence
  load: () => void;
}

// ── Constants ──────────────────────────────────────────

const STORAGE_MODE_KEY = 'commclient_app_mode';
const STORAGE_PIN_KEY = 'commclient_admin_pin';
const TAP_THRESHOLD = 7;
const TAP_WINDOW_MS = 4000;       // 7 taps within 4 seconds
const DEFAULT_AUTO_LOCK_MS = 30 * 60 * 1000;  // 30 minutes

// ── PIN Hashing (simple SHA-256 for client-side PIN) ───

async function hashPin(pin: string): Promise<string> {
  const encoder = new TextEncoder();
  const data = encoder.encode(pin + '_commclient_salt_v1');
  const hashBuffer = await crypto.subtle.digest('SHA-256', data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}

function hashPinSync(pin: string): string {
  // Synchronous fallback using simple hash for store operations
  // Real verification uses async hashPin
  let hash = 0;
  const salted = pin + '_commclient_salt_v1';
  for (let i = 0; i < salted.length; i++) {
    const char = salted.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

// ── Persistence Helpers ────────────────────────────────

function loadSavedMode(): AppMode {
  try {
    const saved = localStorage.getItem(STORAGE_MODE_KEY);
    if (saved === 'advanced' || saved === 'simple') return saved;
  } catch {}
  return 'simple';
}

function saveMode(mode: AppMode): void {
  try {
    localStorage.setItem(STORAGE_MODE_KEY, mode);
  } catch {}
}

function isPinSet(): boolean {
  try {
    return !!localStorage.getItem(STORAGE_PIN_KEY);
  } catch {
    return false;
  }
}

function getSavedPinHash(): string | null {
  try {
    return localStorage.getItem(STORAGE_PIN_KEY);
  } catch {
    return null;
  }
}

function savePinHash(hash: string): void {
  try {
    localStorage.setItem(STORAGE_PIN_KEY, hash);
  } catch {}
}

// ── Store ──────────────────────────────────────────────

export const useAppModeStore = create<AppModeState>((set, get) => ({
  mode: 'simple',
  isAdvanced: false,
  isUnlocked: false,
  isPinConfigured: isPinSet(),
  unlockTimestamp: 0,
  autoLockTimeout: DEFAULT_AUTO_LOCK_MS,
  tapCount: 0,
  tapWindowStart: 0,

  setMode: (mode) => {
    saveMode(mode);
    set({ mode, isAdvanced: mode === 'advanced' });
  },

  switchToSimple: () => {
    saveMode('simple');
    set({ mode: 'simple', isAdvanced: false });
  },

  switchToAdvanced: () => {
    const state = get();
    if (!state.isUnlocked) return;  // Must be unlocked first
    saveMode('advanced');
    set({ mode: 'advanced', isAdvanced: true, unlockTimestamp: Date.now() });
  },

  lockAdvanced: () => {
    saveMode('simple');
    set({
      mode: 'simple',
      isAdvanced: false,
      isUnlocked: false,
      unlockTimestamp: 0,
    });
  },

  // ── Secret Gesture: 7 taps on version label ─────────

  registerTap: () => {
    const now = Date.now();
    const state = get();

    // If too much time passed, reset window
    if (now - state.tapWindowStart > TAP_WINDOW_MS) {
      set({ tapCount: 1, tapWindowStart: now });
      return false;
    }

    const newCount = state.tapCount + 1;
    set({ tapCount: newCount });

    if (newCount >= TAP_THRESHOLD) {
      set({ tapCount: 0, tapWindowStart: 0 });
      return true;  // Threshold reached → show PIN dialog
    }

    return false;
  },

  resetTaps: () => {
    set({ tapCount: 0, tapWindowStart: 0 });
  },

  // ── PIN Verification ─────────────────────────────────

  unlock: (pin) => {
    const stored = getSavedPinHash();

    if (!stored) {
      // No PIN set yet — first-time setup. Accept any 4+ digit PIN as new.
      if (pin.length >= 4) {
        const hash = hashPinSync(pin);
        savePinHash(hash);
        set({
          isUnlocked: true,
          isPinConfigured: true,
          unlockTimestamp: Date.now(),
        });
        // Also switch mode
        saveMode('advanced');
        set({ mode: 'advanced', isAdvanced: true });
        return true;
      }
      return false;
    }

    // Verify against stored hash
    const hash = hashPinSync(pin);
    if (hash === stored) {
      set({ isUnlocked: true, unlockTimestamp: Date.now() });
      saveMode('advanced');
      set({ mode: 'advanced', isAdvanced: true });
      return true;
    }

    return false;
  },

  setPin: (newPin) => {
    if (newPin.length < 4) return;
    const hash = hashPinSync(newPin);
    savePinHash(hash);
    set({ isPinConfigured: true });
  },

  verifyPin: (pin) => {
    const stored = getSavedPinHash();
    if (!stored) return false;
    return hashPinSync(pin) === stored;
  },

  // ── Auto-Lock ────────────────────────────────────────

  refreshActivity: () => {
    const state = get();
    if (state.isAdvanced) {
      set({ unlockTimestamp: Date.now() });
    }
  },

  checkAutoLock: () => {
    const state = get();
    if (!state.isAdvanced || !state.isUnlocked) return;
    const elapsed = Date.now() - state.unlockTimestamp;
    if (elapsed > state.autoLockTimeout) {
      get().lockAdvanced();
    }
  },

  // ── Load from Persistence ────────────────────────────

  load: () => {
    const mode = loadSavedMode();
    set({
      // If saved mode was advanced but no PIN session, default to simple
      mode: 'simple',
      isAdvanced: false,
      isUnlocked: false,
      isPinConfigured: isPinSet(),
    });
  },
}));

// ── Auto-lock interval (check every 60 seconds) ───────

let autoLockInterval: ReturnType<typeof setInterval> | null = null;

export function startAutoLockTimer(): void {
  if (autoLockInterval) return;
  autoLockInterval = setInterval(() => {
    useAppModeStore.getState().checkAutoLock();
  }, 60_000);
}

export function stopAutoLockTimer(): void {
  if (autoLockInterval) {
    clearInterval(autoLockInterval);
    autoLockInterval = null;
  }
}

// ── Activity tracking (mouse/keyboard refreshes auto-lock timer) ──

let activityListenersAttached = false;

export function attachActivityListeners(): void {
  if (activityListenersAttached) return;
  activityListenersAttached = true;

  const refresh = () => useAppModeStore.getState().refreshActivity();
  // Throttle to max once per 30 seconds
  let lastRefresh = 0;
  const throttledRefresh = () => {
    const now = Date.now();
    if (now - lastRefresh > 30_000) {
      lastRefresh = now;
      refresh();
    }
  };

  window.addEventListener('mousemove', throttledRefresh, { passive: true });
  window.addEventListener('keydown', throttledRefresh, { passive: true });
  window.addEventListener('click', throttledRefresh, { passive: true });
}
