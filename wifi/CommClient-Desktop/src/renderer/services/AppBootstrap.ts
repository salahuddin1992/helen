/**
 * AppBootstrap — unified initialization that wires all v2 engines on login
 * and tears them down on logout.
 *
 * Coordinates:
 *   - Socket connection (via socketManager)
 *   - CallEngine init/destroy (via call.store.v2)
 *   - MessagingEngine init/destroy (via chat.store.v2)
 *   - ScreenShareEngine is initialized lazily by CallEngine per-call
 *   - Presence listeners (via contacts.store)
 *   - Settings hydration (via settings.store)
 *
 * Usage:
 *   AppBootstrap.onLogin(userId)   — call after successful auth
 *   AppBootstrap.onLogout()        — call before clearing auth state
 *   AppBootstrap.isReady            — true once all engines are live
 */

import { socketManager } from './socket.manager';
import { AppLogger } from './AppLogger';
import { api } from './api.client';
import { uploadResumeBroker } from './filedrop/UploadResumeBroker';
import { downloadResumeBroker } from './filedrop/DownloadResumeBroker';

// v2 store imports — these expose init/destroy on engine singletons
import { useCallStore } from '../stores/call.store.v2';
import { useChatStore } from '../stores/chat.store.v2';
import { useContactsStore } from '../stores/contacts.store';
import { useSettingsStore } from '../stores/settings.store';
import { useNotificationStore } from '../stores/notification.store';

/** Max age of an abandoned upload session before auto-discard (7 days). */
const RESUMABLE_STALE_MS = 7 * 24 * 60 * 60 * 1000;

const log = AppLogger.create('AppBootstrap');

// ── Types ───────────────────────────────────────────

export interface BootstrapCallbacks {
  /** Called when all engines are initialized and ready */
  onReady?: () => void;
  /** Called if any engine fails to initialize */
  onError?: (module: string, error: Error) => void;
  /** Called when socket reconnects (engines auto-resync) */
  onReconnect?: () => void;
  /** Called when socket connection is lost */
  onDisconnect?: (reason: string) => void;
  /** Called when all reconnect attempts exhausted */
  onReconnectFailed?: () => void;
}

// ── Module State ────────────────────────────────────

let _isReady = false;
let _userId: string | null = null;
let _presenceUnsub: (() => void) | null = null;
let _callbacks: BootstrapCallbacks = {};

// ── Public API ──────────────────────────────────────

export const AppBootstrap = {
  get isReady(): boolean {
    return _isReady;
  },

  get userId(): string | null {
    return _userId;
  },

  /**
   * Initialize all v2 engines after login.
   * Must be called AFTER socketManager.connect().
   */
  onLogin(userId: string, callbacks: BootstrapCallbacks = {}): void {
    if (_isReady && _userId === userId) {
      log.warn('Already initialized for', userId);
      return;
    }

    log.info('Initializing for user:', userId);
    _userId = userId;
    _callbacks = callbacks;

    // ── 1. Settings ──────────────────────────────────
    useSettingsStore.getState().load();

    // ── 2. Contacts + Presence ───────────────────────
    useContactsStore.getState().loadContacts();
    _presenceUnsub = useContactsStore.getState().setupPresenceListeners();

    // ── 3. Call Engine (v2) ──────────────────────────
    try {
      useCallStore.getState().initEngine(userId);
      log.info('CallEngine initialized');
    } catch (e: any) {
      log.error('CallEngine init failed:', e);
      _callbacks.onError?.('CallEngine', e);
    }

    // ── 3b. Push server-side media cap into QualityController ─
    // Fetches the user's effective resolution/bitrate ceiling and the
    // allowed preset ladder from the server. The controller uses this to
    // gate the QualitySelector and clamp forcePreset() calls. If the
    // fetch fails (e.g. older server), the client falls back to its
    // built-in defaults — non-fatal.
    api
      .getMyMediaCap()
      .then(({ cap, ladder }) => {
        useCallStore.getState().applyServerMediaCap(cap, ladder);
        log.info('Media cap applied', cap);
      })
      .catch((e: any) => log.warn('getMyMediaCap failed:', e?.message ?? e));

    // ── 4. Messaging Engine (v2) ─────────────────────
    try {
      useChatStore.getState().initMessaging();
      useChatStore.getState().loadChannels();
      log.info('MessagingEngine initialized');
    } catch (e: any) {
      log.error('MessagingEngine init failed:', e);
      _callbacks.onError?.('MessagingEngine', e);
    }

    // ── 4b. Notifications ────────────────────────────
    try {
      useNotificationStore.getState().fetchUnreadCount();
      log.info('Notification store initialized');
    } catch (e: any) {
      log.error('Notification init failed:', e);
    }

    // ── 4c. Resumable Upload Broker ──────────────────
    // Survey IndexedDB for uploads that were interrupted (crash, quit,
    // network drop) so the UI can offer to resume or discard them.
    uploadResumeBroker
      .scan()
      .then(sessions => {
        if (sessions.length) {
          log.info(`${sessions.length} resumable upload(s) pending`);
        }
        // Prune anything older than the server-side session TTL.
        return uploadResumeBroker.discardStale(RESUMABLE_STALE_MS);
      })
      .then(dropped => {
        if (dropped) log.info(`Discarded ${dropped} stale upload session(s)`);
      })
      .catch(e => log.warn('Resume scan failed:', e?.message ?? e));

    // ── 4d. Resumable Download Broker ────────────────
    // Same startup pattern as uploads but for the download direction —
    // surfaces partial file pulls that can be continued from the last
    // acknowledged byte. Pairs with the server-side HTTP Range support.
    downloadResumeBroker
      .scan()
      .then(downloads => {
        if (downloads.length) {
          log.info(`${downloads.length} resumable download(s) pending`);
        }
        return downloadResumeBroker.discardStale(RESUMABLE_STALE_MS);
      })
      .then(dropped => {
        if (dropped) log.info(`Discarded ${dropped} stale download(s)`);
      })
      .catch(e => log.warn('Download resume scan failed:', e?.message ?? e));

    // ── 4e. Phone Pair Bridge ────────────────────────
    // Receives WebRTC tracks from the user's paired phone (Safari) and
    // registers them as virtual devices so the user can pick "Phone camera"
    // from the normal camera dropdown. Audit fix F2: Vite/ESM bundle
    // doesn't have a runtime require(); use dynamic import which Vite
    // turns into a chunked import-on-demand. Wrapped in IIFE so the
    // outer onLogin can stay synchronous (callers don't await it).
    void (async () => {
      try {
        const mod = await import('./call/PhonePairBridge');
        mod.getPhonePairBridge().start();
        log.info('PhonePairBridge started');
      } catch (e: any) {
        log.warn('PhonePairBridge init failed:', e?.message ?? e);
      }
    })();

    // ── 4f. Phone QT-USB Bridge ──────────────────────
    // Renderer consumer for the main-process QuickTime-over-USB pump.
    // (See above for rationale; F2 also applies here.)
    void (async () => {
      try {
        const mod = await import('./call/PhoneQtBridge');
        mod.startPhoneQtBridge();
        log.info('PhoneQtBridge started');
      } catch (e: any) {
        log.warn('PhoneQtBridge init failed:', e?.message ?? e);
      }
    })();

    // ── 5. Socket reconnect hooks ────────────────────
    socketManager.on('connect', _handleReconnect);
    socketManager.on('disconnect', _handleDisconnect);

    // ── 6. Server shutdown notification ──────────────
    socketManager.on('server:shutdown', _handleServerShutdown);

    _isReady = true;
    log.info('All engines ready — system operational');
    _callbacks.onReady?.();
  },

  /**
   * Tear down all engines before logout.
   */
  onLogout(): void {
    if (!_isReady) return;

    log.info('Shutting down engines...');

    // Unsubscribe presence
    _presenceUnsub?.();
    _presenceUnsub = null;

    // Destroy engines (order: screen share engine is inside CallEngine)
    try {
      useCallStore.getState().destroyEngine();
      log.info('CallEngine destroyed');
    } catch (e) {
      log.error('CallEngine destroy error:', e);
    }

    try {
      useChatStore.getState().destroyMessaging();
      log.info('MessagingEngine destroyed');
    } catch (e) {
      log.error('MessagingEngine destroy error:', e);
    }

    // Audit fix F2: dynamic import in ESM. We don't await — these are
    // teardown calls; failing them on logout shouldn't block the
    // logout flow, just log via the catch.
    void (async () => {
      try {
        const mod = await import('./call/PhonePairBridge');
        mod.getPhonePairBridge().stop();
      } catch { /* ignore */ }
    })();

    void (async () => {
      try {
        const mod = await import('./call/PhoneQtBridge');
        mod.stopPhoneQtBridge();
      } catch { /* ignore */ }
    })();

    // Remove socket hooks
    socketManager.off('connect', _handleReconnect);
    socketManager.off('disconnect', _handleDisconnect);
    socketManager.off('server:shutdown', _handleServerShutdown);

    _isReady = false;
    _userId = null;
    _callbacks = {};
  },
};

// ── Internal Handlers ───────────────────────────────

function _handleReconnect() {
  if (!_isReady) return;
  log.info('Socket reconnected — triggering resync');

  // Messaging engine has built-in resync via SyncManager
  // But also reload contacts and channels for freshness
  useContactsStore.getState().loadContacts();
  useChatStore.getState().fetchChannelSummaries();

  _callbacks.onReconnect?.();
}

function _handleDisconnect(reason: string) {
  log.warn('Socket disconnected', { reason });
  _callbacks.onDisconnect?.(reason);
}

function _handleServerShutdown(data: any) {
  log.warn('Server shutting down', { reason: data?.reason });
  // End any active call gracefully
  try {
    const callState = useCallStore.getState();
    if (callState.status !== 'idle' && callState.status !== 'ended') {
      callState.hangup();
    }
  } catch (e) {
    log.error('Error during server shutdown cleanup', e);
  }
}

export default AppBootstrap;
