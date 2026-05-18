import { create } from 'zustand';
import type { AuthTokens, User } from '../types';
import { api, configureApi, setTokens } from '../services/api.client';
import { socketManager } from '../services/socket.manager';
import { AppBootstrap } from '../services/AppBootstrap';
import * as tokenLifecycle from '../services/tokenLifecycle';

const STORAGE_KEY = 'commclient_auth';

// ── Secure Storage Helpers ──────────────────────────────
// Uses Electron's safeStorage (DPAPI on Windows / Keychain on macOS /
// libsecret on Linux). Production Electron always exposes secureStore
// through the preload bridge — see src/main/index.ts:735.
//
// Audit fix #7: previously we silently fell back to localStorage when
// secureStore wasn't available. That meant a misconfigured packaged
// build (e.g. broken preload) downgraded session tokens to plaintext
// localStorage without any warning. Now we WARN once per session AND
// in the packaged build (`electronAPI` present but `secureStore`
// missing) we refuse to persist instead of silently weakening
// security. Web-only / vitest environments still get the localStorage
// fallback so unit tests don't need a fake secureStore.

const secureStore = (window as any).electronAPI?.secureStore;
// On Android/iOS the renderer has `electronAPI.secure` (Capacitor HelenSecure
// plugin → AES-256-GCM via the system keystore). Treat it the same way we
// treat Electron's safeStorage: required for production credential
// persistence, refuse plaintext fallback when present-but-broken.
const helenSecure = (window as any).electronAPI?.secure;
const isElectronContext = !!(window as any).electronAPI;
const isMobileSecureContext = !!helenSecure && typeof helenSecure.setSecret === 'function';

let _warnedFallback = false;
function _warnFallback(): void {
  if (_warnedFallback) return;
  _warnedFallback = true;
  if (isElectronContext) {
     
    console.error(
      '[auth.store] electronAPI present but secureStore is missing. '
      + 'Refusing to persist credentials in localStorage. Reinstall the app '
      + 'or check the preload script.',
    );
  } else {
     
    console.warn(
      '[auth.store] no Electron secureStore (web/test context); using '
      + 'localStorage fallback. Do NOT ship this configuration to production.',
    );
  }
}

async function saveCredentials(tokens: AuthTokens, serverUrl: string): Promise<void> {
  const payload = JSON.stringify({ tokens, serverUrl });
  if (secureStore) {
    await secureStore.set(STORAGE_KEY, payload);
    return;
  }
  if (isMobileSecureContext) {
    await helenSecure.setSecret({ key: STORAGE_KEY, value: payload });
    return;
  }
  _warnFallback();
  if (isElectronContext) {
    // Production Electron without secureStore = misconfigured. Refuse
    // to persist; the user will have to log in again on next launch
    // but their session token won't sit in plaintext localStorage.
    return;
  }
  localStorage.setItem(STORAGE_KEY, payload);
}

async function loadCredentials(): Promise<{ tokens: AuthTokens; serverUrl: string } | null> {
  try {
    let raw: string | null = null;
    if (secureStore) {
      raw = await secureStore.get(STORAGE_KEY);
    } else if (isMobileSecureContext) {
      const r = await helenSecure.getSecret({ key: STORAGE_KEY });
      raw = (r && r.value) || null;
    } else {
      _warnFallback();
      if (isElectronContext) return null;  // refuse plaintext load too
      raw = localStorage.getItem(STORAGE_KEY);
    }
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed?.tokens?.access_token) return null;
    return parsed;
  } catch {
    return null;
  }
}

async function clearCredentials(): Promise<void> {
  if (secureStore) {
    await secureStore.delete(STORAGE_KEY);
  } else if (isMobileSecureContext) {
    await helenSecure.removeSecret({ key: STORAGE_KEY });
  } else if (!isElectronContext) {
    localStorage.removeItem(STORAGE_KEY);
  }
  // Always best-effort drop legacy localStorage entry too — covers
  // upgrade path where a previous build wrote plaintext.
  try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
}

// ── Server URL (non-sensitive, stays in localStorage) ────

function getSavedServerUrl(): string {
  return localStorage.getItem('commclient_server_url') || 'http://127.0.0.1:3000';
}

/** Rendezvous tunnel URL — e.g. "http://your-vps:9090/t/<public_id>".
 *  Set by the operator in Advanced Settings so the client can reach the
 *  server through the Helen-Rendezvous tunnel when LAN discovery fails
 *  (different network, strict firewall, or when the server is behind
 *  a symmetric NAT). Empty string = not configured. */
function getSavedRendezvousUrl(): string {
  return localStorage.getItem('commclient_rendezvous_url') || '';
}

/**
 * Attempt to resolve the best server URL using discovery.
 * Priority: 1) saved URL (from last session), 2) auto-discovered, 3) default localhost.
 */
async function resolveServerUrl(savedUrl?: string): Promise<string> {
  // If we have a saved URL from a previous session, try it first
  if (savedUrl && savedUrl !== 'http://127.0.0.1:3000') {
    try {
      const resp = await fetch(`${savedUrl}/api/health`, { signal: AbortSignal.timeout(2000) });
      if (resp.ok) return savedUrl;
    } catch {
      // Saved URL unreachable, fall through to discovery
    }
  }

  // Try auto-discovery via Electron IPC
  const discoveryAPI = (window as any).electronAPI?.discovery;
  if (discoveryAPI?.getBest) {
    try {
      const best = await discoveryAPI.getBest();
      if (best?.verified && best?.url) {
        return best.url;
      }
    } catch {}
  }

  // Fallback: localhost (covers dev mode and self-hosted server)
  return savedUrl || 'http://127.0.0.1:3000';
}

// ── Auth Store ──────────────────────────────────────────

interface AuthState {
  user: User | null;
  tokens: AuthTokens | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  serverUrl: string;
  /** Optional rendezvous tunnel URL (http://host:port/t/<public_id>). When
   *  LAN discovery finds nothing, the refresh flow probes this as a
   *  fallback so the server is reachable across different networks. */
  rendezvousUrl: string;

  setServerUrl: (url: string) => void;
  setRendezvousUrl: (url: string) => void;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, displayName: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  restoreSession: () => Promise<boolean>;
  updateUser: (fields: Partial<User>) => void;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  tokens: null,
  isAuthenticated: false,
  isLoading: false,
  error: null,
  serverUrl: getSavedServerUrl(),
  rendezvousUrl: getSavedRendezvousUrl(),

  setRendezvousUrl: (url) => {
    const trimmed = (url || '').trim().replace(/\/+$/, '');
    if (trimmed === '') {
      localStorage.removeItem('commclient_rendezvous_url');
      set({ rendezvousUrl: '' });
      return;
    }
    try {
      const parsed = new URL(trimmed);
      if (!['http:', 'https:'].includes(parsed.protocol)) return;
    } catch { return; }
    localStorage.setItem('commclient_rendezvous_url', trimmed);
    set({ rendezvousUrl: trimmed });
  },

  setServerUrl: (url) => {
    // SECURITY: Validate server URL format
    try {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol)) {
        console.warn('[Auth] Invalid server URL protocol:', parsed.protocol);
        return;
      }
    } catch {
      console.warn('[Auth] Invalid server URL:', url);
      return;
    }
    localStorage.setItem('commclient_server_url', url);
    set({ serverUrl: url });
  },

  login: async (username, password) => {
    set({ isLoading: true, error: null });
    try {
      const serverUrl = get().serverUrl;
      configureApi({
        baseUrl: serverUrl,
        onTokenRefreshed: async (access, refresh) => {
          const tokens = { access_token: access, refresh_token: refresh, token_type: 'bearer', expires_in: 3600 };
          set({ tokens });
          await saveCredentials(tokens, serverUrl);
          setTokens(access, refresh);
          // Update the live socket's auth and re-arm the pre-emptive
          // refresh timer for the next cycle.
          socketManager.updateToken(access);
          tokenLifecycle.arm(access, () => get().tokens?.refresh_token ?? null);
        },
        onAuthFailed: () => get().logout(),
      });

      const data = await api.login({ username, password, device_name: 'Helen Desktop' });
      const { user, tokens } = data;

      setTokens(tokens.access_token, tokens.refresh_token);
      await saveCredentials(tokens, serverUrl);

      socketManager.connect(serverUrl, tokens.access_token, {
        onConnect: () => console.log('[Auth] Socket connected'),
        onDisconnect: (reason) => console.log('[Auth] Socket disconnected:', reason),
      });

      // Pre-emptive token refresh — ensures the access token is rotated
      // BEFORE its exp claim hits, so a long-running call (>access_token_ttl)
      // never sees its bearer expire mid-session.
      tokenLifecycle.arm(
        tokens.access_token,
        () => get().tokens?.refresh_token ?? null,
      );

      set({ user, tokens, isAuthenticated: true, isLoading: false });

      // Initialize all v2 engines via AppBootstrap
      AppBootstrap.onLogin(user.id, {
        onReady: () => console.log('[Auth] AppBootstrap ready'),
        onError: (mod, err) => console.error(`[Auth] ${mod} init failed:`, err),
        onReconnect: () => console.log('[Auth] Engines resynced after reconnect'),
        onDisconnect: (reason) => console.warn('[Auth] Socket lost:', reason),
      });
    } catch (e: any) {
      set({ isLoading: false, error: e.message || 'Login failed' });
      throw e;
    }
  },

  register: async (username, displayName, password) => {
    set({ isLoading: true, error: null });
    try {
      const serverUrl = get().serverUrl;
      // Wire BOTH callbacks before any authenticated call. Previously this
      // path passed only `baseUrl`, so:
      //   - a 401 anywhere in AppBootstrap.onLogin (loadContacts, getMyMediaCap,
      //     loadChannels, fetchUnreadCount …) silently no-op'd the auth-failed
      //     handler instead of triggering logout, leaving the app in a half-
      //     authenticated state where features quietly failed.
      //   - a token refresh fired by tokenLifecycle didn't update the auth
      //     store / persisted credentials, so the next renderer restart
      //     replayed the expired token.
      configureApi({
        baseUrl: serverUrl,
        onTokenRefreshed: async (access, refresh) => {
          const tokens = { access_token: access, refresh_token: refresh, token_type: 'bearer', expires_in: 3600 };
          set({ tokens });
          await saveCredentials(tokens, serverUrl);
          setTokens(access, refresh);
          socketManager.updateToken(access);
          tokenLifecycle.arm(access, () => get().tokens?.refresh_token ?? null);
        },
        onAuthFailed: () => get().logout(),
      });
      const data = await api.register({ username, display_name: displayName, password });
      const { user, tokens } = data;

      setTokens(tokens.access_token, tokens.refresh_token);
      await saveCredentials(tokens, serverUrl);

      socketManager.connect(serverUrl, tokens.access_token);
      tokenLifecycle.arm(
        tokens.access_token,
        () => get().tokens?.refresh_token ?? null,
      );
      set({ user, tokens, isAuthenticated: true, isLoading: false });

      // Initialize all v2 engines via AppBootstrap
      AppBootstrap.onLogin(user.id, {
        onReady: () => console.log('[Auth] AppBootstrap ready (register)'),
        onError: (mod, err) => console.error(`[Auth] ${mod} init failed:`, err),
      });
    } catch (e: any) {
      set({ isLoading: false, error: e.message || 'Registration failed' });
      throw e;
    }
  },

  logout: async () => {
    // Tear down all v2 engines before disconnecting
    AppBootstrap.onLogout();
    tokenLifecycle.cancel();

    // Audit fix M3: tear down the e2eeManager so the next user
    // doesn't inherit identity keys / sessions / pending queue.
    // Lazy import keeps this file independent of the e2ee subsystem
    // for tests / non-Electron contexts.
    void (async () => {
      try {
        const mod = await import('../services/e2ee/E2EEManager');
        // E2EEManager is module-instantiated inside e2ee.store; we
        // don't have a direct handle here. Instead, invoke the store's
        // teardown hook if it exposes one — falls back to no-op.
        const e2eeStore = await import('./e2ee.store');
        const state = (e2eeStore as any).useE2EEStore?.getState?.();
        if (state && typeof state.destroy === 'function') {
          state.destroy();
        }
        void mod;  // imported for side-effect of resolution
      } catch { /* ignore */ }
    })();

    try {
      const tokens = get().tokens;
      if (tokens) await api.logout(tokens.refresh_token).catch(() => {});
    } catch {}
    socketManager.disconnect();
    await clearCredentials();
    set({ user: null, tokens: null, isAuthenticated: false, error: null });
  },

  restoreSession: async () => {
    const stored = await loadCredentials();
    if (!stored) return false;

    const { tokens, serverUrl: savedServerUrl } = stored;

    try {
      // Use discovery to resolve the best server URL
      // (handles IP changes, server restarts on different machine, etc.)
      const serverUrl = await resolveServerUrl(savedServerUrl);
      set({ serverUrl });
      configureApi({
        baseUrl: serverUrl,
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token,
        onTokenRefreshed: async (access, refresh) => {
          const newTokens = { ...tokens, access_token: access, refresh_token: refresh };
          set({ tokens: newTokens });
          await saveCredentials(newTokens, serverUrl);
          setTokens(access, refresh);
          socketManager.updateToken(access);
          tokenLifecycle.arm(access, () => get().tokens?.refresh_token ?? null);
        },
        onAuthFailed: () => get().logout(),
      });
      setTokens(tokens.access_token, tokens.refresh_token);

      const user = await api.getMe();
      socketManager.connect(serverUrl, tokens.access_token);
      tokenLifecycle.arm(
        tokens.access_token,
        () => get().tokens?.refresh_token ?? null,
      );
      set({ user, tokens, isAuthenticated: true, serverUrl });

      // Initialize all v2 engines after session restore
      AppBootstrap.onLogin(user.id, {
        onReady: () => console.log('[Auth] AppBootstrap ready (restored)'),
        onError: (mod, err) => console.error(`[Auth] ${mod} init failed:`, err),
        onReconnect: () => console.log('[Auth] Engines resynced after reconnect'),
        onDisconnect: (reason) => console.warn('[Auth] Socket lost:', reason),
      });

      return true;
    } catch {
      await clearCredentials();
      return false;
    }
  },

  updateUser: (fields) => set((s) => ({ user: s.user ? { ...s.user, ...fields } : null })),
  clearError: () => set({ error: null }),
}));
