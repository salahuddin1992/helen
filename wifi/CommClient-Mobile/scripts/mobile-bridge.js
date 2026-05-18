/**
 * mobile-bridge.js — translates the renderer's window.electronAPI
 * calls into Capacitor plugin calls so the existing React code from
 * CommClient-Desktop runs unchanged inside an Android WebView.
 *
 * Loaded by mobile-bridge injection in scripts/sync-renderer.mjs.
 *
 * What we shim
 * ------------
 *   electronAPI.config.get/set       → @capacitor/preferences
 *   electronAPI.discovery.scan       → custom UDP/HTTP probe
 *   electronAPI.notifications.show   → @capacitor/local-notifications
 *   electronAPI.system.openExternal  → @capacitor/app
 *   electronAPI.network.getInterfaces → @capacitor/network
 *   electronAPI.system.getInfo       → @capacitor/device
 *
 * Anything Electron-specific that has no mobile equivalent (e.g.
 * embedded server spawn, native menus, IPC ports) is replaced with
 * a no-op that logs a debug warning so the renderer doesn't crash.
 */
(function installMobileBridge() {
  if (typeof window === 'undefined') return;
  if (window.__helenMobileBridgeInstalled) return;
  window.__helenMobileBridgeInstalled = true;

  // Capacitor exposes its plugin registry on window.Capacitor.
  // We lazy-load each plugin via dynamic import from the bundled
  // CDN-style URL Capacitor sets up under capacitor:// in WebView.
  const cap = () => (window.Capacitor || {});
  const isNative = () => cap().isNativePlatform && cap().isNativePlatform();

  const debug = (...args) => {
    if (!window.__helenMobileBridgeQuiet) {
      console.debug('[mobile-bridge]', ...args);
    }
  };

  async function pluginCall(pluginName, method, opts) {
    const c = cap();
    if (!c.Plugins || !c.Plugins[pluginName]) {
      debug(`plugin ${pluginName} not registered — no-op`);
      return null;
    }
    return c.Plugins[pluginName][method](opts || {});
  }

  // ── Persistent storage shim ───────────────────────────────
  // The renderer expects synchronous-ish settings via electronAPI.
  // Capacitor Preferences is async; we use an in-memory cache that
  // is hydrated on first call and written-through on every set.
  const prefsCache = new Map();
  let prefsHydrated = false;
  async function hydratePrefs() {
    if (prefsHydrated) return;
    try {
      const c = cap();
      if (c.Plugins && c.Plugins.Preferences) {
        const { keys } = await c.Plugins.Preferences.keys();
        for (const k of keys || []) {
          const { value } = await c.Plugins.Preferences.get({ key: k });
          try { prefsCache.set(k, JSON.parse(value)); }
          catch { prefsCache.set(k, value); }
        }
      }
    } catch (e) {
      debug('hydratePrefs failed:', e);
    }
    prefsHydrated = true;
  }

  async function prefsSet(key, value) {
    prefsCache.set(key, value);
    try {
      await pluginCall('Preferences', 'set', {
        key,
        value: typeof value === 'string' ? value : JSON.stringify(value),
      });
    } catch (e) { debug('prefs set failed:', e); }
  }

  async function prefsGet(key, defaultValue) {
    if (!prefsHydrated) await hydratePrefs();
    return prefsCache.has(key) ? prefsCache.get(key) : defaultValue;
  }

  // ── Server discovery shim ─────────────────────────────────
  // Desktop uses UDP broadcast on 41234 + mDNS. WebView can't open
  // raw sockets, so we fall back to:
  //   1. Last-used URL from preferences (instant)
  //   2. HTTP probe of common LAN ranges (slow, only on first run)
  //   3. Manual entry via the renderer's onboarding screen
  async function discoveryScan({ timeoutMs = 5000 } = {}) {
    const last = await prefsGet('helen.lastServerUrl');
    if (last) {
      try {
        const r = await fetch(`${last}/api/health`, {
          method: 'GET',
          signal: AbortSignal.timeout(2000),
        });
        if (r.ok) return [{ url: last, source: 'last_used', latency_ms: 0 }];
      } catch { /* fall through to scan */ }
    }
    // Probe the device's own subnet by guessing common gateway addresses.
    // We can't enumerate interfaces from WebView, so we try the typical
    // LAN ranges in parallel with a short timeout.
    const candidates = [
      'http://192.168.1.1:3000',
      'http://192.168.0.1:3000',
      'http://192.168.1.100:3000',
      'http://192.168.0.100:3000',
      'http://10.0.0.1:3000',
      'http://10.0.0.100:3000',
    ];
    const results = await Promise.allSettled(
      candidates.map(async (url) => {
        const t0 = performance.now();
        const r = await fetch(`${url}/api/health`, {
          signal: AbortSignal.timeout(timeoutMs),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = await r.json();
        if (body.service !== 'Helen Server') throw new Error('not helen');
        return { url, source: 'lan_probe', latency_ms: performance.now() - t0 };
      }),
    );
    return results
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value);
  }

  // ── window.electronAPI assembly ───────────────────────────
  window.electronAPI = window.electronAPI || {
    platform: 'android',
    isMobile: true,

    config: {
      get: (key, def) => prefsGet(key, def),
      set: (key, value) => prefsSet(key, value),
      delete: (key) => {
        prefsCache.delete(key);
        return pluginCall('Preferences', 'remove', { key });
      },
      getAll: async () => {
        if (!prefsHydrated) await hydratePrefs();
        return Object.fromEntries(prefsCache);
      },
    },

    discovery: {
      scan: discoveryScan,
      stop: () => Promise.resolve(),
    },

    notifications: {
      show: ({ title, body, tag } = {}) =>
        pluginCall('LocalNotifications', 'schedule', {
          notifications: [{
            id: Math.floor(Math.random() * 100000),
            title: title || 'Helen',
            body: body || '',
            extra: { tag },
          }],
        }),
      requestPermission: () =>
        pluginCall('LocalNotifications', 'requestPermissions'),
    },

    system: {
      openExternal: (url) => pluginCall('App', 'openUrl', { url }),
      getInfo: async () => {
        const dev = await pluginCall('Device', 'getInfo');
        return {
          platform: 'android',
          arch: (dev || {}).platform || 'arm64',
          version: (dev || {}).osVersion || 'unknown',
          model: (dev || {}).model || 'unknown',
        };
      },
      copyToClipboard: async (text) => {
        // No Capacitor plugin without an extra install; use the
        // Web Clipboard API as a fallback.
        if (navigator.clipboard) await navigator.clipboard.writeText(text);
      },
    },

    network: {
      getStatus: () => pluginCall('Network', 'getStatus'),
      onChange: (cb) => {
        const c = cap();
        if (!c.Plugins || !c.Plugins.Network) return () => {};
        const handle = c.Plugins.Network.addListener('networkStatusChange', cb);
        return () => handle.remove && handle.remove();
      },
    },

    // ── Native call lifecycle (HelenCall Capacitor plugin) ──
    // Lets the renderer hand off "I'm in a call" to a real Android
    // foreground service so the OS doesn't kill the WebView when the
    // user backgrounds the app, and post heads-up notifications for
    // inbound calls with Accept/Decline actions.
    call: {
      /** @param {{ channelId:string, peerName?:string, isVideo?:boolean }} opts */
      startActive: (opts) => pluginCall('HelenCall', 'startActiveCall', opts),
      stopActive:  ()     => pluginCall('HelenCall', 'stopActiveCall'),
      isOnCall:    ()     => pluginCall('HelenCall', 'isOnCall'),
      /** @param {{ callerName:string, callerId:string, channelId:string, isVideo?:boolean }} opts
       *  @returns {Promise<{notifId:number}>} */
      notifyIncoming: async (opts) => {
        const r = await pluginCall('HelenCall', 'notifyIncomingCall', opts);
        if (r && typeof r.notifId === 'number') {
          window.__helenLastIncomingNotifId = r.notifId;
        }
        return r;
      },
      /** @param {{ notifId?:number }} [opts] — defaults to the last
       *  notifId returned from notifyIncoming, so callers can dismiss
       *  the active heads-up without tracking IDs themselves. */
      cancelIncoming: async (opts) => {
        const id = (opts && opts.notifId) || window.__helenLastIncomingNotifId;
        if (!id || id <= 0) return null;
        const r = await pluginCall('HelenCall', 'cancelIncomingCall',
                                   { notifId: id });
        window.__helenLastIncomingNotifId = undefined;
        return r;
      },
      acquireMulticastLock: () =>
        pluginCall('HelenCall', 'acquireMulticastLock'),
      releaseMulticastLock: () =>
        pluginCall('HelenCall', 'releaseMulticastLock'),
      requestNotificationsPermission: () =>
        pluginCall('HelenCall', 'requestNotificationsPermission'),

      /**
       * Subscribe to incoming-call decisions made from the heads-up
       * notification (Accept / Decline). The receiver fires a
       * `helen://call/<decision>?channelId=…&callerId=…&isVideo=…`
       * deep-link which Capacitor surfaces as an `appUrlOpen` event.
       *
       * @param {(d:{decision:'accept'|'decline', channelId?:string,
       *           callerId?:string, isVideo?:boolean}) => void} cb
       * @returns {() => void} unsubscribe
       */
      onIncomingDecision: (cb) => {
        const c = cap();
        if (!c.Plugins || !c.Plugins.App) return () => {};
        const handle = c.Plugins.App.addListener('appUrlOpen', (ev) => {
          try {
            const u = new URL(ev.url);
            if (u.protocol !== 'helen:' || u.hostname !== 'call') return;
            const decision = u.pathname.replace(/^\/+/, '');
            if (decision !== 'accept' && decision !== 'decline') return;
            cb({
              decision,
              channelId: u.searchParams.get('channelId') || undefined,
              callerId:  u.searchParams.get('callerId')  || undefined,
              isVideo:   u.searchParams.get('isVideo') === 'true',
            });
          } catch (e) {
            debug('onIncomingDecision parse failed:', e);
          }
        });
        return () => handle.remove && handle.remove();
      },
    },

    // ── Encrypted secret store + biometric gate (HelenSecure) ──
    // AES-256-GCM via Android Keystore. Used for JWT pair, never for
    // settings (those go to plain Preferences). Falls back to plaintext
    // SharedPreferences only if the device's keystore is unusable —
    // info() reports `encrypted:false` so the renderer can warn the user.
    secure: {
      /** @param {{ key:string, value:string }} opts */
      setSecret:    (opts) => pluginCall('HelenSecure', 'setSecret', opts),
      /** @param {{ key:string }} opts → { value: string|null } */
      getSecret:    (opts) => pluginCall('HelenSecure', 'getSecret', opts),
      /** @param {{ key:string }} opts */
      removeSecret: (opts) => pluginCall('HelenSecure', 'removeSecret', opts),
      clearAll:     ()     => pluginCall('HelenSecure', 'clearAll'),
      /** → { encrypted: boolean, hardwareBacked: boolean } */
      info:         ()     => pluginCall('HelenSecure', 'info'),

      /** → { available: boolean, reason?: string } */
      canUseBiometrics: () =>
        pluginCall('HelenSecure', 'canUseBiometrics'),
      /** Show BiometricPrompt. Resolves with { authenticated, errorCode?, errorMessage? }.
       * @param {{ title?:string, subtitle?:string, reason?:string,
       *           allowDeviceCredential?:boolean }} [opts] */
      authenticate: (opts) =>
        pluginCall('HelenSecure', 'authenticate', opts || {}),
    },

    // ── Offline message retry queue (HelenWorker) ──
    // Wraps androidx.work.WorkManager. Survives process death — the
    // queue lives in WM's internal DB, not on our heap. Constraint:
    // NetworkType.CONNECTED. Backoff: exponential, capped at 5 attempts.
    worker: {
      /** @param {{ baseUrl:string, bearer:string, channelId:string,
       *           content:string, type?:string, clientMessageId?:string }} opts
       *  @returns {Promise<{ queued:true, clientMessageId:string }>} */
      queueMessageRetry: (opts) =>
        pluginCall('HelenWorker', 'queueMessageRetry', opts),
      cancelAllRetries: () =>
        pluginCall('HelenWorker', 'cancelAllRetries'),
    },

    // ── Telecom ConnectionService bridge (HelenConnection) ──
    // Self-managed PhoneAccount on API 26+. Lets Helen calls participate
    // in system audio focus, Bluetooth headset controls, hold-on-GSM
    // collision, Android Auto, and Wear OS — without replacing the
    // dialer. Methods resolve { supported:false } on older devices.
    connection: {
      isSupported:           ()     => pluginCall('HelenConnection', 'isSupported'),
      registerPhoneAccount:  ()     => pluginCall('HelenConnection', 'registerPhoneAccount'),
      unregisterPhoneAccount:()     => pluginCall('HelenConnection', 'unregisterPhoneAccount'),
      /** @param {{ channelId:string, peerName?:string, isVideo?:boolean }} opts */
      placeOutgoingCall:     (opts) => pluginCall('HelenConnection', 'placeOutgoingCall', opts),
      /** @param {{ channelId:string, callerName?:string, isVideo?:boolean }} opts */
      notifyIncomingCall:    (opts) => pluginCall('HelenConnection', 'notifyIncomingCall', opts),

      /**
       * Subscribe to Telecom Connection events (answer/reject/disconnect/
       * hold/unhold/abort + audio-route switches). The Service broadcasts
       * each event as `helen://telecom/<event>?audio=…&state=…&cause=…`,
       * picked up here through Capacitor's `appUrlOpen`.
       *
       * @param {(d:{event:string, audio?:string, state?:string,
       *           cause?:number}) => void} cb
       * @returns {() => void} unsubscribe
       */
      onTelecomEvent: (cb) => {
        const c = cap();
        if (!c.Plugins || !c.Plugins.App) return () => {};
        const handle = c.Plugins.App.addListener('appUrlOpen', (ev) => {
          try {
            const u = new URL(ev.url);
            if (u.protocol !== 'helen:' || u.hostname !== 'telecom') return;
            cb({
              event: u.pathname.replace(/^\/+/, ''),
              audio: u.searchParams.get('audio') || undefined,
              state: u.searchParams.get('state') || undefined,
              cause: u.searchParams.get('cause')
                       ? Number(u.searchParams.get('cause'))
                       : undefined,
            });
          } catch (e) {
            debug('onTelecomEvent parse failed:', e);
          }
        });
        return () => handle.remove && handle.remove();
      },
    },

    // ── App Shortcuts (long-press launcher) routing ──
    // Static shortcuts in res/xml/shortcuts.xml fire `helen://shortcut/<id>`
    // when tapped. The renderer can subscribe here to react (focus
    // a tab, open a modal, etc.) on cold start AND warm resume.
    shortcuts: {
      /** @param {(id:'new-chat'|'search-contacts'|'start-call'|'recents') => void} cb */
      onShortcut: (cb) => {
        const c = cap();
        if (!c.Plugins || !c.Plugins.App) return () => {};
        const handle = c.Plugins.App.addListener('appUrlOpen', (ev) => {
          try {
            const u = new URL(ev.url);
            if (u.protocol !== 'helen:' || u.hostname !== 'shortcut') return;
            cb(u.pathname.replace(/^\/+/, ''));
          } catch (e) {
            debug('onShortcut parse failed:', e);
          }
        });
        return () => handle.remove && handle.remove();
      },
    },

    // No-ops with debug logging for desktop-only APIs. The renderer's
    // codebase calls these from menus / system tray paths that don't
    // exist on mobile; rather than throw, we no-op so the app keeps
    // running and the affected feature degrades gracefully.
    server: {
      start: () => { debug('server.start ignored on mobile'); return Promise.resolve(false); },
      stop:  () => { debug('server.stop ignored on mobile');  return Promise.resolve(); },
      status: () => Promise.resolve({ running: false, mobile: true }),
    },
    menu: {
      show: () => debug('menu.show ignored on mobile'),
      setLabel: () => debug('menu.setLabel ignored on mobile'),
    },
    window: {
      minimize: () => debug('window.minimize ignored on mobile'),
      close:    () => pluginCall('App', 'exitApp'),
      reload:   () => location.reload(),
    },
  };

  // Hint to the React layer that we're a mobile WebView so layout
  // can switch to mobile-first breakpoints even if the user-agent
  // string isn't enough.
  document.documentElement.setAttribute('data-runtime', 'mobile');
  document.documentElement.setAttribute('data-platform', 'android');

  debug('installed');
})();
