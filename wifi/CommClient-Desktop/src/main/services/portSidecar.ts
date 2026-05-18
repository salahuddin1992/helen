/**
 * Port Sidecar Resolver — Connectivity Hotfix Layer (Module A, main side).
 *
 * Purpose
 * -------
 * The bundled Helen server (`run.py`) picks a free TCP port at startup
 * (tier 1: 3000-3010, tier 2: 3088-3100, tier 3: ephemeral) and writes
 * the final value to:
 *
 *     %APPDATA%/CommClient/data/.helen-server.port
 *
 * Historically the Electron parent assumed port 3000 and the renderer
 * hard-coded `http://127.0.0.1:3000`. The moment two Helen builds are
 * installed side-by-side (or another app squats on 3000) the desktop
 * client silently talks to the wrong process. This module turns the
 * sidecar file into the single source of truth for the bundled server's
 * effective URL.
 *
 * Design
 * ------
 * - Pure side-effect-free I/O on top of `fs.watchFile` — no native
 *   watcher, works on every Windows/macOS/Linux flavour without
 *   `chokidar`-style transitive deps.
 * - Exponential backoff retry for "file not yet written" startup race
 *   (Electron main can start in well under 50 ms; Python takes ~2-4 s).
 * - Exposes an `EventEmitter` so the IPC layer can fan-out change events
 *   to every renderer without re-reading the file from each window.
 * - Singleton: a single watcher is shared across the whole main process
 *   regardless of how many modules import it.
 *
 * Side effects
 * ------------
 * None on import. The watcher is lazily armed on the first call to
 * `getCurrentPort()` / `watchPort()` / `resolveServerUrl()`.
 */

import { promises as fsp, watchFile, unwatchFile, existsSync } from 'node:fs';
import { join } from 'node:path';
import { EventEmitter } from 'node:events';
import { app } from 'electron';

/** Sidecar filename written by `CommClient-Server/run.py`. */
const SIDECAR_FILENAME = '.helen-server.port';

/** Subdirectory under `%APPDATA%/CommClient/` where the server keeps data. */
const SIDECAR_SUBDIR = 'data';

/** Sentinel min/max for a sane TCP port. */
const MIN_PORT = 1;
const MAX_PORT = 65535;

/** Default initial backoff (ms) when the sidecar is missing. */
const RETRY_BASE_MS = 100;
/** Cap on the exponential backoff between retries. */
const RETRY_MAX_MS = 5_000;
/** Total attempts before `getCurrentPort()` gives up and returns null. */
const RETRY_MAX_ATTEMPTS = 20;
/** `watchFile` poll interval. 750 ms is well under any UX-visible delay. */
const WATCH_INTERVAL_MS = 750;

export interface ResolveServerUrlOptions {
    /** Override host. Defaults to 127.0.0.1 (the bundled server is loopback-bound by default). */
    host?: string;
    /** Override protocol. Defaults to "http". Set "https" once TLS is on. */
    protocol?: 'http' | 'https';
    /** Hard fallback port used when the sidecar is unreadable. Defaults to 3000. */
    fallbackPort?: number;
    /** When true, never wait — return synchronously with the cached value or null. */
    noWait?: boolean;
}

/** Disposable returned by `watchPort` — call to unsubscribe. */
export interface Disposable {
    dispose: () => void;
}

/** Internal cached state, kept private to the singleton. */
interface CacheState {
    port: number | null;
    /** Wall-clock timestamp of the last successful read. */
    readAt: number;
    /** Path resolved once at first use, then cached. */
    path: string;
}

const emitter = new EventEmitter();
// Allow many subscribers (renderers, IPC bridge, internal callers).
emitter.setMaxListeners(32);

let cache: CacheState | null = null;
let watching = false;

/** Resolve the absolute path to the sidecar file. */
function resolveSidecarPath(): string {
    // Prefer Electron's app.getPath('appData') — it accounts for the
    // portable APPDATA override and for non-Windows hosts. We append the
    // CommClient/data subtree manually because the server writes that
    // exact layout (see run.py: BASE_DIR + 'data' + '.helen-server.port').
    const appData = app.getPath('appData');
    return join(appData, 'CommClient', SIDECAR_SUBDIR, SIDECAR_FILENAME);
}

function logInfo(msg: string, extra?: Record<string, unknown>): void {
    // Keep logging dependency-free — index.ts uses console.* throughout
    // and pipes stdout/stderr to a rotating file, so this is consistent.
    const tag = '[portSidecar]';
    if (extra) console.log(tag, msg, extra);
    else console.log(tag, msg);
}

function logWarn(msg: string, extra?: Record<string, unknown>): void {
    const tag = '[portSidecar]';
    if (extra) console.warn(tag, msg, extra);
    else console.warn(tag, msg);
}

/** Parse the sidecar file content into a validated port number or null. */
function parsePortFile(raw: string): number | null {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    const n = Number.parseInt(trimmed, 10);
    if (!Number.isFinite(n) || n < MIN_PORT || n > MAX_PORT) return null;
    return n;
}

/** Read the sidecar file once. Returns null on any failure mode. */
async function readPortFileOnce(path: string): Promise<number | null> {
    try {
        const raw = await fsp.readFile(path, 'utf-8');
        return parsePortFile(raw);
    } catch {
        return null;
    }
}

/**
 * Read the sidecar with exponential backoff for transient absence
 * (server still booting). The retry chain caps at `RETRY_MAX_ATTEMPTS`
 * iterations so a permanently-missing file fails the call instead of
 * hanging the renderer forever.
 */
async function readPortFileWithBackoff(path: string): Promise<number | null> {
    let delay = RETRY_BASE_MS;
    for (let attempt = 0; attempt < RETRY_MAX_ATTEMPTS; attempt++) {
        const port = await readPortFileOnce(path);
        if (port !== null) return port;
        await new Promise<void>((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 1.7, RETRY_MAX_MS);
    }
    return null;
}

/** Install `fs.watchFile` polling. Idempotent — safe to call repeatedly. */
function armWatcher(path: string): void {
    if (watching) return;
    watching = true;
    watchFile(path, { interval: WATCH_INTERVAL_MS, persistent: false }, async (curr, prev) => {
        // mtimeMs is 0 when the file does not exist; ignore those transitions
        // unless the file was previously present (i.e. it was deleted).
        if (curr.mtimeMs === prev.mtimeMs && curr.size === prev.size) return;
        const next = await readPortFileOnce(path);
        const previous = cache?.port ?? null;
        if (next === previous) return;
        cache = { port: next, readAt: Date.now(), path };
        logInfo('sidecar changed', { previous, next, path });
        emitter.emit('change', { port: next, previous, path });
    });
    logInfo('watcher armed', { path });
}

/** Tear down the watcher. Used during graceful shutdown. */
function disarmWatcher(): void {
    if (!watching || !cache) return;
    try {
        unwatchFile(cache.path);
    } catch (err) {
        logWarn('unwatchFile failed', { error: (err as Error).message });
    }
    watching = false;
}

/**
 * Initial path resolution + cache hydration. Separated from the public
 * API so `resolveServerUrl` can hot-path on the cache without redoing it.
 */
async function ensureHydrated(): Promise<CacheState> {
    if (cache && cache.port !== null) return cache;
    const path = cache?.path ?? resolveSidecarPath();
    const port = await readPortFileWithBackoff(path);
    cache = { port, readAt: Date.now(), path };
    armWatcher(path);
    return cache;
}

/**
 * Public — returns the currently-cached port number, hydrating on first
 * use. Resolves to null if the sidecar has not been (and cannot be) read.
 *
 * Safe to call from anywhere in the main process. Concurrent callers
 * share the same in-flight read (the underlying I/O is idempotent so we
 * don't bother with a single-flight guard).
 */
export async function getCurrentPort(): Promise<number | null> {
    const state = await ensureHydrated();
    return state.port;
}

/**
 * Subscribe to sidecar changes. Returns a `Disposable` whose `dispose()`
 * method unhooks the listener. The watcher itself remains armed for
 * other subscribers.
 */
export function watchPort(
    cb: (event: { port: number | null; previous: number | null; path: string }) => void,
): Disposable {
    // Hydrate immediately so the first emit reflects current state if
    // the caller subscribes after the initial read.
    void ensureHydrated();
    emitter.on('change', cb);
    return {
        dispose: () => {
            try { emitter.off('change', cb); } catch { /* ignore */ }
        },
    };
}

/**
 * Compose a full server URL from the resolved port plus host/protocol
 * options. Used by the renderer-side resolver before every HTTP call.
 *
 * Fallback ladder:
 *   1. Cached sidecar port.
 *   2. Fresh sidecar read with backoff (skipped when `noWait` is true).
 *   3. `opts.fallbackPort` (default 3000) — last-resort, lets the app
 *      boot when the sidecar genuinely cannot be created.
 */
export async function resolveServerUrl(
    opts: ResolveServerUrlOptions = {},
): Promise<string> {
    const host = opts.host ?? '127.0.0.1';
    const protocol = opts.protocol ?? 'http';
    const fallbackPort = opts.fallbackPort ?? 3000;

    let port: number | null = cache?.port ?? null;
    if (port === null && !opts.noWait) {
        port = (await ensureHydrated()).port;
    } else if (port === null && opts.noWait) {
        // Re-arm the watcher even if we won't wait, so a later change
        // event can repair the URL without restarting the process.
        if (!watching) armWatcher(cache?.path ?? resolveSidecarPath());
    }

    const finalPort = port ?? fallbackPort;
    return `${protocol}://${host}:${finalPort}`;
}

/**
 * Optional — force re-read the sidecar file on demand. Useful from
 * IPC handlers when the renderer wants to bypass the watcher's poll
 * interval (e.g. user clicked "Retry" after a connection failure).
 */
export async function refresh(): Promise<number | null> {
    const path = cache?.path ?? resolveSidecarPath();
    const port = await readPortFileOnce(path);
    const previous = cache?.port ?? null;
    cache = { port, readAt: Date.now(), path };
    if (port !== previous) {
        emitter.emit('change', { port, previous, path });
    }
    armWatcher(path);
    return port;
}

/** Returns the resolved absolute path of the sidecar file. */
export function getSidecarPath(): string {
    return cache?.path ?? resolveSidecarPath();
}

/** Returns true when the sidecar file exists right now (sync probe). */
export function sidecarExistsSync(): boolean {
    const path = cache?.path ?? resolveSidecarPath();
    return existsSync(path);
}

/** Tear down the watcher — call from `before-quit` if desired. */
export function dispose(): void {
    emitter.removeAllListeners('change');
    disarmWatcher();
    cache = null;
}

/** Re-export the singleton emitter for advanced callers (tests, diagnostics). */
export const portSidecarEvents: EventEmitter = emitter;

const portSidecar = {
    getCurrentPort,
    watchPort,
    resolveServerUrl,
    refresh,
    getSidecarPath,
    sidecarExistsSync,
    dispose,
    events: emitter,
};

export default portSidecar;
