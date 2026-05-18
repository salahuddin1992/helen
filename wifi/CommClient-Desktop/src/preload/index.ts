/**
 * Preload script — exposes a safe API from main process to renderer via contextBridge.
 *
 * All IPC channels are explicitly whitelisted. No arbitrary channel access.
 */
import { contextBridge, ipcRenderer } from 'electron';

// Whitelist of shortcut channels the renderer can listen to
const ALLOWED_SHORTCUT_CHANNELS = [
  'shortcut:toggle-mute',
  'shortcut:toggle-video',
  'shortcut:toggle-deafen',
  'shortcut:end-call',
] as const;

const electronAPI = {
  // ── Window Controls (frameless window) ──────────────
  window: {
    minimize: () => ipcRenderer.invoke('window:minimize'),
    maximize: () => ipcRenderer.invoke('window:maximize'),
    close: () => ipcRenderer.invoke('window:close'),
    isMaximized: (): Promise<boolean> => ipcRenderer.invoke('window:is-maximized'),
    toggleAlwaysOnTop: (): Promise<boolean> =>
      ipcRenderer.invoke('window:toggle-always-on-top'),
    setAlwaysOnTop: (on: boolean): Promise<boolean> =>
      ipcRenderer.invoke('window:set-always-on-top', on),
    setCompact: (on: boolean): Promise<boolean> =>
      ipcRenderer.invoke('window:set-compact', on),
  },

  // ── Screen Capture (for screen sharing) ─────────────
  getDesktopSources: (): Promise<Array<{
    id: string;
    name: string;
    thumbnail: string;
    appIcon: string | null;
    display_id: string;
  }>> => ipcRenderer.invoke('desktop-capturer:get-sources'),

  // ── App Info ────────────────────────────────────────
  getVersion: (): Promise<string> => ipcRenderer.invoke('app:get-version'),
  getServerPort: (): Promise<number> => ipcRenderer.invoke('app:get-server-port'),
  getDataDir: (): Promise<string> => ipcRenderer.invoke('app:get-data-dir'),
  isDev: (): Promise<boolean> => ipcRenderer.invoke('app:is-dev'),
  /**
   * Central client config — drives the renderer's connection logic.
   * In production, serverUrl is the single canonical URL; renderer must
   * NOT probe alternative ports or auto-switch on discovery events.
   */
  getClientConfig: (): Promise<{
    mode: 'production' | 'development' | 'standalone';
    serverUrl: string;
    allowEmbeddedServer: boolean;
    allowLanDiscovery: boolean;
    allowAutoServerSwitch: boolean;
    deviceId: string;
    /** Last-10 hex chars of the SMBIOS Machine UUID. Shown after the
     *  username so peers can disambiguate `helen` on machine A from
     *  `helen` on machine B (e.g. `helen#0B222F2F85`). */
    deviceTag: string;
  }> => ipcRenderer.invoke('app:get-client-config'),
  getDisplayName: (): Promise<string> => ipcRenderer.invoke('app:get-display-name'),
  setDisplayName: (name: string): Promise<{ success: boolean; name?: string; error?: string }> =>
    ipcRenderer.invoke('app:set-display-name', name),

  // ── Notifications ───────────────────────────────────
  showNotification: (title: string, body: string) => {
    ipcRenderer.send('notification:show', { title, body });
  },
  /**
   * Force the main window to the foreground. Used when an incoming
   * DM / group message / call needs to pull the user back into the
   * app even if they're working in another window. No-op when the
   * window is already focused.
   */
  forceFocusWindow: () => {
    ipcRenderer.send('window:force-focus');
  },
  /**
   * Push the current unread count to the OS-level surfaces (window
   * title prefix, tray tooltip, taskbar/dock badge). Call on every
   * unread-count change. Returns the clamped value the main process
   * actually applied.
   */
  setUnreadBadge: (count: number): Promise<number> =>
    ipcRenderer.invoke('app:set-unread-badge', count),

  // ── PIP Call Window ─────────────────────────────────
  callWindow: {
    open: () => ipcRenderer.invoke('call-window:open'),
    close: () => ipcRenderer.invoke('call-window:close'),
  },

  // ── Global Shortcut Listeners ───────────────────────
  onShortcut: (channel: string, callback: () => void): (() => void) => {
    // Security: only allow whitelisted shortcut channels
    if (!ALLOWED_SHORTCUT_CHANNELS.includes(channel as any)) {
      console.warn(`[Preload] Blocked shortcut listener for unknown channel: ${channel}`);
      return () => {};
    }
    const handler = () => callback();
    ipcRenderer.on(channel, handler);
    return () => ipcRenderer.removeListener(channel, handler);
  },

  // ── Secure Credential Storage (OS-level encryption) ──
  secureStore: {
    set: (key: string, value: string): Promise<boolean> =>
      ipcRenderer.invoke('secure-store:set', key, value),
    get: (key: string): Promise<string | null> =>
      ipcRenderer.invoke('secure-store:get', key),
    delete: (key: string): Promise<boolean> =>
      ipcRenderer.invoke('secure-store:delete', key),
    clear: (): Promise<boolean> =>
      ipcRenderer.invoke('secure-store:clear'),
  },

  // ── LAN Discovery ──────────────────────────────────
  discovery: {
    getServers: (): Promise<any[]> =>
      ipcRenderer.invoke('discovery:get-servers'),
    getBest: (): Promise<any | null> =>
      ipcRenderer.invoke('discovery:get-best'),
    addManual: (url: string): Promise<any | null> =>
      ipcRenderer.invoke('discovery:add-manual', url),
    // Resolve a 64-char server code to a discovered server. Waits up to
    // timeoutMs for a matching UDP broadcast (default 8s).
    findByCode: (code: string, timeoutMs?: number): Promise<any | null> =>
      ipcRenderer.invoke('discovery:find-by-code', code, timeoutMs),
    refresh: (): Promise<any[]> =>
      ipcRenderer.invoke('discovery:refresh'),
    isListening: (): Promise<boolean> =>
      ipcRenderer.invoke('discovery:is-listening'),
    getNetworkStatus: (): Promise<{
      hasNetwork: boolean;
      reconnectAttempts: number;
      isListening: boolean;
      serverCount: number;
      verifiedCount: number;
    }> => ipcRenderer.invoke('discovery:get-network-status'),
    restart: (): Promise<boolean> =>
      ipcRenderer.invoke('discovery:restart'),
    // Force TCP scan of the whole LAN — mandatory-connection fallback when
    // UDP broadcast is blocked by firewall/guest WiFi.
    activeScan: (): Promise<{
      scanned: number;
      found: number;
      liveTcpHits: number;
      subnets: string[];
      durationMs: number;
    }> => ipcRenderer.invoke('discovery:active-scan'),
    // Sequential LAN-only fallback chain. Short-circuits at the first
    // verified server; operator can retry individual methods on demand.
    lanOrch: {
      run: (): Promise<any> => ipcRenderer.invoke('lan-orch:run'),
      snapshot: (): Promise<any> => ipcRenderer.invoke('lan-orch:snapshot'),
      retry: (method: string): Promise<any> =>
        ipcRenderer.invoke('lan-orch:retry', method),
      abort: (): Promise<boolean> => ipcRenderer.invoke('lan-orch:abort'),
    },
    onServersUpdated: (callback: (servers: any[]) => void): (() => void) => {
      const handler = (_event: any, servers: any[]) => callback(servers);
      ipcRenderer.on('discovery:servers-updated', handler);
      return () => ipcRenderer.removeListener('discovery:servers-updated', handler);
    },
    onNetworkStatus: (callback: (status: { status: string; attempt: number }) => void): (() => void) => {
      const handler = (_event: any, data: { status: string; attempt: number }) => callback(data);
      ipcRenderer.on('discovery:network-status', handler);
      return () => ipcRenderer.removeListener('discovery:network-status', handler);
    },
  },

  // ── Diagnostics ─────────────────────────────────────
  diagnostics: {
    /** Write serialized log entries to a rotating log file on disk */
    writeDiagnosticLog: (entries: string[]): Promise<void> =>
      ipcRenderer.invoke('diagnostics:write-log', entries),
    /** Open a Save dialog and write the diagnostics JSON package */
    saveDiagnosticsPackage: (jsonData: string, suggestedFileName: string): Promise<string | null> =>
      ipcRenderer.invoke('diagnostics:save-package', jsonData, suggestedFileName),
  },

  // Convenience aliases expected by DiagnosticsLogger / DiagnosticsCollector
  writeDiagnosticLog: (entries: string[]): Promise<void> =>
    ipcRenderer.invoke('diagnostics:write-log', entries),
  saveDiagnosticsPackage: (jsonData: string, suggestedFileName: string): Promise<string | null> =>
    ipcRenderer.invoke('diagnostics:save-package', jsonData, suggestedFileName),

  // ── Platform Info ───────────────────────────────────
  platform: process.platform as 'win32' | 'darwin' | 'linux',

  // ── OS Integration (Phase 2) ────────────────────────
  system: {
    info: (): Promise<any> => ipcRenderer.invoke('system:info'),

    // Deep links (commclient://...)
    getInitialDeepLink: (): Promise<any> => ipcRenderer.invoke('system:deep-link:get-initial'),
    flushDeepLink: (): Promise<any> => ipcRenderer.invoke('system:deep-link:flush'),
    onDeepLink: (cb: (payload: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('system:deep-link', h);
      return () => ipcRenderer.removeListener('system:deep-link', h);
    },

    // Power events
    onPower: (cb: (evt: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('system:power', h);
      return () => ipcRenderer.removeListener('system:power', h);
    },
    powerState: (): Promise<any> => ipcRenderer.invoke('system:power:state'),

    // Auto-start
    autostart: {
      get: (): Promise<any> => ipcRenderer.invoke('system:autostart:get'),
      set: (enabled: boolean): Promise<any> =>
        ipcRenderer.invoke('system:autostart:set', enabled),
    },
  },

  // ── Notifications (Phase 2) ─────────────────────────
  notifications: {
    show: (payload: {
      title: string;
      body: string;
      silent?: boolean;
      actions?: Array<{ text: string; deepLink?: string; id?: string }>;
      clickDeepLink?: string;
      clickId?: string;
      tag?: string;
    }): Promise<any> => ipcRenderer.invoke('notifications:show', payload),
    supported: (): Promise<boolean> => ipcRenderer.invoke('notifications:supported'),
    onClick: (cb: (p: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('notification:click', h);
      return () => ipcRenderer.removeListener('notification:click', h);
    },
    onAction: (cb: (p: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('notification:action', h);
      return () => ipcRenderer.removeListener('notification:action', h);
    },
  },

  // ── Tray ────────────────────────────────────────────
  tray: {
    setUnread: (count: number): Promise<any> => ipcRenderer.invoke('tray:set-unread', count),
    setDnd: (enabled: boolean): Promise<any> => ipcRenderer.invoke('tray:set-dnd', enabled),
    flash: (reason?: string): Promise<any> => ipcRenderer.invoke('tray:flash', reason),
    onNavigate: (cb: (p: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('tray:navigate', h);
      return () => ipcRenderer.removeListener('tray:navigate', h);
    },
    onDnd: (cb: (p: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('tray:dnd', h);
      return () => ipcRenderer.removeListener('tray:dnd', h);
    },
    onUpdateCheck: (cb: () => void): (() => void) => {
      const h = () => cb();
      ipcRenderer.on('tray:update-check', h);
      return () => ipcRenderer.removeListener('tray:update-check', h);
    },
  },

  // ── USB iPhone (Personal Hotspot → USB) ────────────
  usbPhone: {
    getStatus: (): Promise<{
      connected: boolean;
      hostAddress: string | null;
      phoneAddress: string | null;
      interfaceName: string | null;
      mac: string | null;
      since: number;
    }> => ipcRenderer.invoke('phone:get-usb-status'),
    onStatus: (cb: (status: {
      connected: boolean;
      hostAddress: string | null;
      phoneAddress: string | null;
      interfaceName: string | null;
      mac: string | null;
      since: number;
    }) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('phone:usb-status', h);
      return () => ipcRenderer.removeListener('phone:usb-status', h);
    },
    // QuickTime-over-USB helper (scaffold). Always callable; returns
    // ``supported: false`` on installs without the native ``usb`` module,
    // so renderers can feature-gate gracefully.
    qt: {
      getStatus: (): Promise<{
        supported: boolean;
        error: string | null;
        devices: Array<{
          udid: string;
          product: string;
          vendorId: number;
          productId: number;
          streaming: boolean;
        }>;
        lastScan: number;
      }> => ipcRenderer.invoke('phone:qt-get-status'),
      activate: (udid: string): Promise<{ ok: boolean; error?: string }> =>
        ipcRenderer.invoke('phone:qt-activate', udid),
      stop: (udid: string): Promise<{ ok: boolean }> =>
        ipcRenderer.invoke('phone:qt-stop', udid),
      onStatus: (cb: (status: any) => void): (() => void) => {
        const h = (_e: any, p: any) => cb(p);
        ipcRenderer.on('phone:qt-status', h);
        return () => ipcRenderer.removeListener('phone:qt-status', h);
      },
      // Raw bulk-in packets streamed from the phone. Each callback receives
      // a chunk from the QT IN endpoint — consumers are expected to assemble
      // the QT frame format in a worker and decode H.264 NALUs on the
      // renderer side. Chunks are Buffers (delivered as Uint8Array).
      onFrame: (cb: (payload: { udid: string; data: Uint8Array }) => void): (() => void) => {
        const h = (_e: any, p: any) => cb(p);
        ipcRenderer.on('phone:qt-frame', h);
        return () => ipcRenderer.removeListener('phone:qt-frame', h);
      },
      // Write a buffer to the phone's bulk-OUT endpoint. Used by the renderer
      // state machine to drive the QT handshake (ping replies, sync/async
      // responses) — without this the phone never starts sending A/V.
      send: (udid: string, data: Uint8Array): Promise<{ ok: boolean; bytes?: number; error?: string }> =>
        ipcRenderer.invoke('phone:qt-send', udid, data),
    },
  },

  // ── Native call lifecycle (mobile-only on Android; no-op on desktop) ──
  // The renderer uses these to hand off "I'm in a call" to the platform's
  // native foreground service so the OS doesn't kill the WebView when
  // backgrounded, and to post heads-up incoming-call notifications. On
  // desktop these are no-ops since Electron windows never get backgrounded
  // by the OS in a way that would kill WebRTC.
  call: {
    startActive: async (_opts: { channelId: string; peerName?: string; isVideo?: boolean }):
      Promise<{ started: boolean }> => ({ started: false }),
    stopActive: async (): Promise<{ stopped: boolean }> => ({ stopped: false }),
    isOnCall:   async (): Promise<{ active: boolean }>  => ({ active: false }),
    notifyIncoming: async (_opts: {
      callerName: string; callerId: string; channelId: string; isVideo?: boolean;
    }): Promise<{ notifId: number }> => ({ notifId: -1 }),
    cancelIncoming: async (_opts?: { notifId?: number }): Promise<void> => {},
    acquireMulticastLock: async (): Promise<void> => {},
    releaseMulticastLock: async (): Promise<void> => {},
    requestNotificationsPermission: async (): Promise<{ granted: boolean }> => ({ granted: true }),
    onIncomingDecision: (_cb: (d: {
      decision: 'accept' | 'decline';
      channelId?: string;
      callerId?: string;
      isVideo?: boolean;
    }) => void): (() => void) => () => {},
  },

  // ── Encrypted secret store + biometric gate (mobile-only on Android) ──
  // On desktop: secrets pass through to non-encrypted electron-store, and
  // biometric methods report "no-hardware" so the renderer skips the gate.
  secure: {
    setSecret:    async (_opts: { key: string; value: string }): Promise<void> => {},
    getSecret:    async (_opts: { key: string }): Promise<{ value: string | null }> =>
      ({ value: null }),
    removeSecret: async (_opts: { key: string }): Promise<void> => {},
    clearAll:     async (): Promise<void> => {},
    info:         async (): Promise<{ encrypted: boolean; hardwareBacked: boolean }> =>
      ({ encrypted: false, hardwareBacked: false }),
    canUseBiometrics: async (): Promise<{ available: boolean; reason?: string }> =>
      ({ available: false, reason: 'desktop' }),
    authenticate: async (_opts?: {
      title?: string; subtitle?: string; reason?: string; allowDeviceCredential?: boolean;
    }): Promise<{ authenticated: boolean; errorCode?: number; errorMessage?: string }> =>
      ({ authenticated: true }),   // desktop: skip the gate
  },

  // ── Offline retry queue (mobile-only on Android; no-op on desktop) ──
  worker: {
    queueMessageRetry: async (_opts: {
      baseUrl: string; bearer: string; channelId: string;
      content: string; type?: string; clientMessageId?: string;
    }): Promise<{ queued: boolean; clientMessageId: string }> =>
      ({ queued: false, clientMessageId: '' }),
    cancelAllRetries: async (): Promise<void> => {},
  },

  // ── Telecom ConnectionService (mobile-only on Android API 26+) ──
  connection: {
    isSupported:            async (): Promise<{ supported: boolean; apiLevel?: number }> =>
      ({ supported: false }),
    registerPhoneAccount:   async (): Promise<{ registered: boolean; reason?: string; accountId?: string }> =>
      ({ registered: false, reason: 'desktop' }),
    unregisterPhoneAccount: async (): Promise<void> => {},
    placeOutgoingCall:      async (_opts: { channelId: string; peerName?: string; isVideo?: boolean }):
      Promise<{ placed: boolean; reason?: string }> => ({ placed: false, reason: 'desktop' }),
    notifyIncomingCall:     async (_opts: { channelId: string; callerName?: string; isVideo?: boolean }):
      Promise<{ notified: boolean; reason?: string }> => ({ notified: false, reason: 'desktop' }),
    onTelecomEvent: (_cb: (d: {
      event: string; audio?: string; state?: string; cause?: number;
    }) => void): (() => void) => () => {},
  },

  // ── App Shortcuts (mobile-only) ──
  shortcuts: {
    onShortcut: (_cb: (id: string) => void): (() => void) => () => {},
  },

  // ── Downloads (save-to-disk + open-with-default) ────
  // Used by chat bubbles to download a video/file to the user's
  // Downloads folder and then either play it in-app or hand it
  // off to the system default application.
  downloads: {
    /** Save raw bytes to the user's Downloads folder under
     *  a sanitized filename. Returns the absolute path written. */
    saveBuffer: (
      filename: string,
      bytes: ArrayBuffer,
    ): Promise<{ ok: boolean; path?: string; error?: string }> =>
      ipcRenderer.invoke('downloads:save-buffer', filename, bytes),
    /** Stream a remote URL to disk with progress events. Returns
     *  the absolute path on completion. The caller can subscribe to
     *  progress via ``onProgress(downloadId, cb)``. */
    streamUrl: (
      url: string,
      filename: string,
      bearerToken?: string,
    ): Promise<{
      ok: boolean;
      path?: string;
      bytes?: number;
      error?: string;
    }> => ipcRenderer.invoke(
      'downloads:stream-url', url, filename, bearerToken,
    ),
    /** Launch the file with the OS default application
     *  (shell.openPath under the hood — no shell-injection risk
     *  because Electron's API takes a literal path, not a command). */
    openPath: (absPath: string): Promise<string> =>
      ipcRenderer.invoke('downloads:open-path', absPath),
    /** Reveal the file in the OS file explorer (Finder/Explorer/Nautilus). */
    revealInFolder: (absPath: string): Promise<void> =>
      ipcRenderer.invoke('downloads:reveal', absPath),
    /** Subscribe to per-download progress events. Returns an
     *  unsubscribe function. */
    onProgress: (
      cb: (p: {
        url: string;
        bytes_received: number;
        bytes_total: number | null;
      }) => void,
    ): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('downloads:progress', h);
      return () => ipcRenderer.removeListener('downloads:progress', h);
    },
  },

  // ── Auto-Updater ────────────────────────────────────
  updater: {
    status: (): Promise<any> => ipcRenderer.invoke('updater:get-status'),
    check: (): Promise<any> => ipcRenderer.invoke('updater:check'),
    installNow: (): Promise<any> => ipcRenderer.invoke('updater:install-now'),
    setChannel: (channel: 'stable' | 'beta' | 'canary'): Promise<any> =>
      ipcRenderer.invoke('updater:set-channel', channel),
    onStatus: (cb: (p: any) => void): (() => void) => {
      const h = (_e: any, p: any) => cb(p);
      ipcRenderer.on('updater:status', h);
      return () => ipcRenderer.removeListener('updater:status', h);
    },
  },
};

contextBridge.exposeInMainWorld('electronAPI', electronAPI);

export type ElectronAPI = typeof electronAPI;
