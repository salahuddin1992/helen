/**
 * Port Resolver — Connectivity Hotfix Layer (Module A, renderer side).
 *
 * Purpose
 * -------
 * Every HTTP / WebSocket / Socket.IO call from the renderer must use
 * the port chosen by the bundled Helen server at startup, not the
 * hard-coded 3000. This module hides the IPC + caching glue behind a
 * minimal API:
 *
 *     const baseUrl = await resolveBaseUrl();
 *     // -> "http://127.0.0.1:3088"  (whatever run.py picked)
 *
 * Caching
 * -------
 * - In-memory: the resolved URL is kept in a module-scoped variable so
 *   tight loops never round-trip through IPC.
 * - localStorage: a short-TTL (default 60 s) cache key survives reloads
 *   and acts as a fallback when the preload bridge has not yet wired
 *   the IPC channel (extremely rare race window during cold start).
 *
 * Change propagation
 * ------------------
 * `subscribeToChanges()` registers a callback that fires whenever the
 * main process re-broadcasts a sidecar change. Consumers (API client,
 * Socket.IO manager) use this to re-issue their `configureApi({baseUrl})`
 * call without forcing a full app reload.
 *
 * Safety
 * ------
 * - Uses `window.helenPortSidecar` (exposed via the preload bridge);
 *   gracefully falls through to a localStorage / hard-coded default
 *   when the bridge is absent (e.g. running in a browser unit test).
 * - Never throws. Always resolves to a valid `http://host:port` string.
 */

const STORAGE_KEY = 'helen.portResolver.cache.v1';
const DEFAULT_TTL_MS = 60_000;
const HARD_FALLBACK_URL = 'http://127.0.0.1:3000';

/** Shape of the cache row persisted to localStorage. */
interface PersistedCache {
    /** Full resolved URL: `${protocol}://${host}:${port}`. */
    baseUrl: string;
    /** Last refresh timestamp (epoch ms). */
    at: number;
    /** TTL window (ms) — entries older than this are ignored. */
    ttl: number;
}

interface ResolverState {
    baseUrl: string | null;
    refreshedAt: number;
    /** Single-flight guard for concurrent `resolveBaseUrl()` callers. */
    inflight: Promise<string> | null;
    /** Whether the IPC subscription has been armed. */
    subscribed: boolean;
}

const state: ResolverState = {
    baseUrl: null,
    refreshedAt: 0,
    inflight: null,
    subscribed: false,
};

type ChangeCallback = (newUrl: string) => void;
const listeners = new Set<ChangeCallback>();

/** Shape of the preload-injected bridge. Keep in sync with portSidecar.bridge.ts. */
interface HelenPortSidecarBridge {
    get(): Promise<{ port: number | null; path: string; exists: boolean }>;
    resolveUrl(opts?: {
        host?: string;
        protocol?: 'http' | 'https';
        fallbackPort?: number;
        noWait?: boolean;
    }): Promise<string>;
    subscribe(
        cb: (payload: { port: number | null; previous: number | null }) => void,
    ): () => void;
}

declare global {
    interface Window {
        helenPortSidecar?: HelenPortSidecarBridge;
    }
}

function isBrowser(): boolean {
    return typeof window !== 'undefined' && typeof localStorage !== 'undefined';
}

function readPersisted(): PersistedCache | null {
    if (!isBrowser()) return null;
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw) as Partial<PersistedCache>;
        if (
            typeof parsed.baseUrl !== 'string'
            || !/^https?:\/\/[^\s]+:\d+/.test(parsed.baseUrl)
            || typeof parsed.at !== 'number'
            || typeof parsed.ttl !== 'number'
        ) {
            return null;
        }
        return parsed as PersistedCache;
    } catch {
        return null;
    }
}

function writePersisted(baseUrl: string, ttl = DEFAULT_TTL_MS): void {
    if (!isBrowser()) return;
    try {
        const payload: PersistedCache = { baseUrl, at: Date.now(), ttl };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch {
        // localStorage may be full or sandboxed — non-fatal.
    }
}

function cachedStillFresh(cache: PersistedCache): boolean {
    return Date.now() - cache.at <= cache.ttl;
}

function ensureSubscribed(): void {
    if (state.subscribed) return;
    const bridge = window?.helenPortSidecar;
    if (!bridge) return;
    try {
        bridge.subscribe(async () => {
            // Force a fresh resolve on every change broadcast — the IPC
            // payload is advisory, the canonical answer still comes from
            // `resolveUrl` so any host/protocol normalisation stays in
            // one place (the main process).
            try {
                const next = await bridge.resolveUrl({});
                if (next && next !== state.baseUrl) {
                    state.baseUrl = next;
                    state.refreshedAt = Date.now();
                    writePersisted(next);
                    listeners.forEach((cb) => {
                        try { cb(next); } catch { /* ignore */ }
                    });
                }
            } catch {
                // Bridge transiently unavailable; ignore.
            }
        });
        state.subscribed = true;
    } catch {
        // Subscription may fail in non-Electron contexts — fall through.
    }
}

/**
 * Resolve the canonical base URL for the bundled Helen server.
 *
 * Resolution order:
 *   1. In-memory cache (always trusted within process lifetime).
 *   2. `window.helenPortSidecar.resolveUrl()` via IPC.
 *   3. Fresh localStorage cache row (≤ TTL).
 *   4. Stale localStorage cache row (best-effort fallback).
 *   5. Hard-coded `http://127.0.0.1:3000`.
 *
 * Always returns a syntactically valid URL — never throws.
 */
export async function resolveBaseUrl(): Promise<string> {
    if (state.baseUrl) {
        ensureSubscribed();
        return state.baseUrl;
    }
    if (state.inflight) return state.inflight;

    state.inflight = (async (): Promise<string> => {
        try {
            ensureSubscribed();

            // Path 1 — IPC. Trust the main process completely.
            const bridge = window?.helenPortSidecar;
            if (bridge) {
                try {
                    const url = await bridge.resolveUrl({});
                    if (typeof url === 'string' && /^https?:\/\/[^\s]+:\d+/.test(url)) {
                        state.baseUrl = url;
                        state.refreshedAt = Date.now();
                        writePersisted(url);
                        return url;
                    }
                } catch {
                    // Fall through to localStorage / hard fallback.
                }
            }

            // Path 2 — fresh localStorage row.
            const persisted = readPersisted();
            if (persisted && cachedStillFresh(persisted)) {
                state.baseUrl = persisted.baseUrl;
                state.refreshedAt = persisted.at;
                return persisted.baseUrl;
            }

            // Path 3 — stale localStorage row as best-effort fallback.
            if (persisted) {
                state.baseUrl = persisted.baseUrl;
                state.refreshedAt = persisted.at;
                return persisted.baseUrl;
            }

            // Path 4 — hard fallback. Lets the app finish booting even
            // when the sidecar machinery is entirely unavailable.
            state.baseUrl = HARD_FALLBACK_URL;
            state.refreshedAt = Date.now();
            return HARD_FALLBACK_URL;
        } finally {
            state.inflight = null;
        }
    })();

    return state.inflight;
}

/**
 * Synchronously read whatever the resolver believes is the current
 * base URL. Returns null if `resolveBaseUrl()` has never been awaited.
 *
 * Useful for code that runs in event handlers and cannot await before
 * issuing a request (e.g. inside a websocket reconnect callback).
 */
export function getCachedBaseUrl(): string | null {
    return state.baseUrl;
}

/**
 * Force the resolver to forget its in-memory cache. The next call to
 * `resolveBaseUrl()` will round-trip through IPC.
 */
export function invalidate(): void {
    state.baseUrl = null;
    state.refreshedAt = 0;
    if (isBrowser()) {
        try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
    }
}

/**
 * Register a callback for base-URL changes (e.g. when the bundled
 * server restarts on a different port). Returns an unsubscribe handle.
 */
export function subscribeToChanges(cb: ChangeCallback): () => void {
    listeners.add(cb);
    ensureSubscribed();
    return () => { listeners.delete(cb); };
}

/**
 * Compose an absolute URL by joining a path onto the resolved base.
 * Convenience helper used by callers that don't want to manually
 * concatenate.
 */
export async function withBase(path: string): Promise<string> {
    const base = await resolveBaseUrl();
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    const sep = path.startsWith('/') ? '' : '/';
    return base.replace(/\/+$/, '') + sep + path;
}

const portResolver = {
    resolveBaseUrl,
    getCachedBaseUrl,
    invalidate,
    subscribeToChanges,
    withBase,
};

export default portResolver;
