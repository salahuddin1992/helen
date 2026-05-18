/**
 * ProductShell.ts — Final app chrome, window management, and system integration.
 *
 * This service manages the "product wrapper" around the core app:
 *   - Window state (size, position, maximize, minimize to tray)
 *   - System tray behavior (minimize to tray, tray menu)
 *   - Desktop notifications (native OS notifications)
 *   - Global keyboard shortcuts
 *   - Window title (dynamic: "CommClient — Chatting with X")
 *   - Taskbar badge (unread count on Windows)
 *   - Auto-start on login (optional)
 *   - Close behavior (minimize to tray vs. actually quit)
 *   - Idle detection (auto-away after 5 minutes)
 *   - Focus tracking (mark channels as read when focused)
 *   - Startup splash timing
 *
 * Design:
 *   - Uses Electron IPC via preload bridge (never imports Electron directly)
 *   - All Electron-dependent features degrade gracefully if IPC unavailable
 *   - Title bar text follows product naming: "CommClient" (never shows URLs or ports)
 */

import { AppLogger } from '../AppLogger';

const log = AppLogger.create('ProductShell');

// ── Types ───────────────────────────────────────────────────

export type CloseAction = 'minimize_to_tray' | 'quit';
export type PresenceState = 'online' | 'away' | 'busy' | 'offline';

export interface ProductShellConfig {
  closeAction: CloseAction;
  autoStart: boolean;
  idleTimeoutMinutes: number;
  showInTaskbar: boolean;
  enableDesktopNotifications: boolean;
  enableSoundEffects: boolean;
  minimizeToTrayOnStart: boolean;
}

export interface WindowInfo {
  title: string;
  isMaximized: boolean;
  isFocused: boolean;
  isMinimized: boolean;
}

// ── Electron API Interface ──────────────────────────────────

interface ElectronWindowAPI {
  minimize?: () => void;
  maximize?: () => void;
  close?: () => void;
  isMaximized?: () => Promise<boolean>;
}

interface ElectronNotifyAPI {
  show?: (title: string, body: string) => void;
}

// ── Main Service ────────────────────────────────────────────

class ProductShellService {
  private config: ProductShellConfig;
  private appName = 'Helen';
  private currentTitle = 'Helen';
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private presenceState: PresenceState = 'online';
  private activityListeners: Array<() => void> = [];
  private focusListeners: Array<(focused: boolean) => void> = [];
  private presenceListeners: Array<(state: PresenceState) => void> = [];

  constructor() {
    this.config = this.loadConfig();
  }

  // ── Configuration ───────────────────────────────────────

  private loadConfig(): ProductShellConfig {
    const defaults: ProductShellConfig = {
      closeAction: 'minimize_to_tray',
      autoStart: false,
      idleTimeoutMinutes: 5,
      showInTaskbar: true,
      enableDesktopNotifications: true,
      enableSoundEffects: true,
      minimizeToTrayOnStart: false,
    };

    try {
      const saved = localStorage.getItem('commclient_shell_config');
      if (saved) return { ...defaults, ...JSON.parse(saved) };
    } catch {}

    return defaults;
  }

  getConfig(): ProductShellConfig {
    return { ...this.config };
  }

  updateConfig(partial: Partial<ProductShellConfig>): void {
    this.config = { ...this.config, ...partial };
    try {
      localStorage.setItem('commclient_shell_config', JSON.stringify(this.config));
    } catch {}
    log.info('Shell config updated', partial);
  }

  // ── Lifecycle ───────────────────────────────────────────

  /**
   * Initialize shell behaviors. Call once at app startup.
   */
  start(): void {
    this.setupIdleDetection();
    this.setupFocusTracking();
    // Pull the live display name from the Electron main (falls back to 'Helen').
    try {
      (window as any)?.electronAPI?.getDisplayName?.().then((n: string) => {
        if (n && typeof n === 'string') {
          this.appName = n;
          this.updateTitle();
        }
      });
    } catch {}
    this.updateTitle();
    log.info('ProductShell started');
  }

  stop(): void {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
    // Remove activity listeners
    for (const cleanup of this.activityListeners) {
      cleanup();
    }
    this.activityListeners = [];
  }

  // ── Window Title ────────────────────────────────────────
  // Dynamic title: "CommClient — Chatting with Ahmad"

  updateTitle(subtitle?: string): void {
    this.currentTitle = subtitle ? `${this.appName} — ${subtitle}` : this.appName;
    try {
      document.title = this.currentTitle;
    } catch {}
  }

  getTitle(): string {
    return this.currentTitle;
  }

  // ── Window Controls ─────────────────────────────────────

  minimize(): void {
    try {
      (window as any).electronAPI?.minimize?.();
    } catch {}
  }

  maximize(): void {
    try {
      (window as any).electronAPI?.maximize?.();
    } catch {}
  }

  close(): void {
    try {
      if (this.config.closeAction === 'minimize_to_tray') {
        (window as any).electronAPI?.minimize?.();
      } else {
        (window as any).electronAPI?.close?.();
      }
    } catch {}
  }

  quit(): void {
    try {
      (window as any).electronAPI?.close?.();
    } catch {}
  }

  // ── Desktop Notifications ───────────────────────────────

  showNotification(title: string, body: string): void {
    if (!this.config.enableDesktopNotifications) return;

    try {
      (window as any).electronAPI?.notify?.(title, body);
    } catch {
      // Fallback to web Notification API
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(title, { body });
      }
    }
  }

  // ── Taskbar Badge (Unread Count) ────────────────────────

  updateBadgeCount(count: number): void {
    try {
      // Electron's app.setBadgeCount on Windows uses overlay icon
      (window as any).electronAPI?.setBadgeCount?.(count);
    } catch {}
  }

  // ── Idle Detection ──────────────────────────────────────
  // Auto-set presence to "away" after N minutes of no activity.

  private setupIdleDetection(): void {
    const resetIdle = () => {
      if (this.presenceState === 'away') {
        this.setPresence('online');
      }
      this.restartIdleTimer();
    };

    const events = ['mousemove', 'keydown', 'mousedown', 'touchstart', 'scroll'];
    for (const event of events) {
      const handler = () => resetIdle();
      window.addEventListener(event, handler, { passive: true });
      this.activityListeners.push(() => window.removeEventListener(event, handler));
    }

    this.restartIdleTimer();
  }

  private restartIdleTimer(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);

    const timeoutMs = this.config.idleTimeoutMinutes * 60 * 1000;
    this.idleTimer = setTimeout(() => {
      if (this.presenceState === 'online') {
        log.info('User idle, setting presence to away');
        this.setPresence('away');
      }
    }, timeoutMs);
  }

  // ── Focus Tracking ──────────────────────────────────────

  private setupFocusTracking(): void {
    const handleFocus = () => {
      for (const cb of this.focusListeners) {
        try { cb(true); } catch {}
      }
    };
    const handleBlur = () => {
      for (const cb of this.focusListeners) {
        try { cb(false); } catch {}
      }
    };

    window.addEventListener('focus', handleFocus);
    window.addEventListener('blur', handleBlur);
    this.activityListeners.push(() => {
      window.removeEventListener('focus', handleFocus);
      window.removeEventListener('blur', handleBlur);
    });
  }

  /**
   * Subscribe to window focus changes. Returns unsubscribe.
   */
  onFocusChange(callback: (focused: boolean) => void): () => void {
    this.focusListeners.push(callback);
    return () => {
      this.focusListeners = this.focusListeners.filter((cb) => cb !== callback);
    };
  }

  // ── Presence ────────────────────────────────────────────

  getPresence(): PresenceState {
    return this.presenceState;
  }

  setPresence(state: PresenceState): void {
    if (this.presenceState === state) return;
    this.presenceState = state;
    for (const cb of this.presenceListeners) {
      try { cb(state); } catch {}
    }
  }

  onPresenceChange(callback: (state: PresenceState) => void): () => void {
    this.presenceListeners.push(callback);
    return () => {
      this.presenceListeners = this.presenceListeners.filter((cb) => cb !== callback);
    };
  }

  // ── Sound Effects ───────────────────────────────────────

  async playSound(type: 'message' | 'call_ring' | 'call_end' | 'notification' | 'connect' | 'disconnect'): Promise<void> {
    if (!this.config.enableSoundEffects) return;

    // Sound files would be in public/sounds/
    const soundMap: Record<string, string> = {
      message: '/sounds/message.mp3',
      call_ring: '/sounds/ring.mp3',
      call_end: '/sounds/call-end.mp3',
      notification: '/sounds/notification.mp3',
      connect: '/sounds/connect.mp3',
      disconnect: '/sounds/disconnect.mp3',
    };

    const src = soundMap[type];
    if (!src) return;

    try {
      const audio = new Audio(src);
      audio.volume = 0.5;
      await audio.play();
    } catch {
      // Sound playback failed (e.g., no audio device, user hasn't interacted)
    }
  }
}

// ── Singleton ───────────────────────────────────────────────

export const productShell = new ProductShellService();
