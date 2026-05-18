/**
 * Port Sidecar IPC handlers — Connectivity Hotfix Layer (Module A, IPC).
 *
 * Bridges `services/portSidecar.ts` to renderer processes via three
 * stable IPC channels:
 *
 *   port-sidecar:get          — one-shot probe of the current state.
 *   port-sidecar:resolve-url  — full URL composition with options.
 *   port-sidecar:subscribe    — renderer asks to receive change events.
 *
 * Every subscribing renderer receives `port-sidecar:changed` broadcasts
 * with the new port and previous port, so they can rebuild HTTP
 * clients / sockets without reloading the window.
 *
 * No sensitive state crosses the bridge. Even the file path is harmless
 * — it's `%APPDATA%/CommClient/data/.helen-server.port`, the same
 * location both processes already know.
 */

import { ipcMain, webContents, type IpcMainInvokeEvent } from 'electron';
import portSidecar, {
    getCurrentPort,
    resolveServerUrl,
    refresh,
    getSidecarPath,
    sidecarExistsSync,
    watchPort,
    type ResolveServerUrlOptions,
} from '../services/portSidecar.js';

/** IPC channel names — keep in sync with the preload bridge + renderer. */
export const IPC_CHANNELS = {
    GET: 'port-sidecar:get',
    RESOLVE_URL: 'port-sidecar:resolve-url',
    SUBSCRIBE: 'port-sidecar:subscribe',
    UNSUBSCRIBE: 'port-sidecar:unsubscribe',
    CHANGED: 'port-sidecar:changed',
    REFRESH: 'port-sidecar:refresh',
} as const;

/** Subscriber registry — keyed by WebContents id so a dead window cleans up. */
const subscribers = new Map<number, { sender: Electron.WebContents }>();

/** Single watcher disposable shared across the whole subscriber pool. */
let watchDisposable: { dispose: () => void } | null = null;

/** Validate and coerce IPC-provided options to a safe shape. */
function sanitizeResolveOpts(raw: unknown): ResolveServerUrlOptions {
    if (!raw || typeof raw !== 'object') return {};
    const obj = raw as Record<string, unknown>;
    const out: ResolveServerUrlOptions = {};
    if (typeof obj.host === 'string' && /^[A-Za-z0-9.\-_:]{1,255}$/.test(obj.host)) {
        out.host = obj.host;
    }
    if (obj.protocol === 'http' || obj.protocol === 'https') {
        out.protocol = obj.protocol;
    }
    if (
        typeof obj.fallbackPort === 'number'
        && Number.isFinite(obj.fallbackPort)
        && obj.fallbackPort > 0
        && obj.fallbackPort < 65536
    ) {
        out.fallbackPort = obj.fallbackPort;
    }
    if (typeof obj.noWait === 'boolean') out.noWait = obj.noWait;
    return out;
}

function logInfo(msg: string, extra?: Record<string, unknown>): void {
    const tag = '[portSidecar.ipc]';
    if (extra) console.log(tag, msg, extra);
    else console.log(tag, msg);
}

function logWarn(msg: string, extra?: Record<string, unknown>): void {
    const tag = '[portSidecar.ipc]';
    if (extra) console.warn(tag, msg, extra);
    else console.warn(tag, msg);
}

/** Lazily arm the single shared watcher when the first renderer subscribes. */
function ensureWatchArmed(): void {
    if (watchDisposable) return;
    watchDisposable = watchPort((event) => {
        // Fan-out to every registered renderer. Skip destroyed contents.
        for (const [id, entry] of subscribers) {
            const wc = entry.sender;
            if (wc.isDestroyed()) {
                subscribers.delete(id);
                continue;
            }
            try {
                wc.send(IPC_CHANNELS.CHANGED, {
                    port: event.port,
                    previous: event.previous,
                });
            } catch (err) {
                logWarn('broadcast failed', { id, error: (err as Error).message });
            }
        }
    });
}

/** Public — wire up every channel. Idempotent. */
export function installPortSidecarHandlers(): void {
    // ── GET ─────────────────────────────────────────────
    ipcMain.handle(IPC_CHANNELS.GET, async (_evt: IpcMainInvokeEvent) => {
        try {
            const port = await getCurrentPort();
            return {
                port,
                path: getSidecarPath(),
                exists: sidecarExistsSync(),
            };
        } catch (err) {
            logWarn('get failed', { error: (err as Error).message });
            return {
                port: null,
                path: getSidecarPath(),
                exists: sidecarExistsSync(),
            };
        }
    });

    // ── RESOLVE-URL ─────────────────────────────────────
    ipcMain.handle(IPC_CHANNELS.RESOLVE_URL, async (
        _evt: IpcMainInvokeEvent,
        rawOpts: unknown,
    ) => {
        try {
            const opts = sanitizeResolveOpts(rawOpts);
            return await resolveServerUrl(opts);
        } catch (err) {
            logWarn('resolve-url failed', { error: (err as Error).message });
            // Hard fallback — the renderer must always receive a usable URL.
            return 'http://127.0.0.1:3000';
        }
    });

    // ── REFRESH ─────────────────────────────────────────
    ipcMain.handle(IPC_CHANNELS.REFRESH, async () => {
        try {
            const port = await refresh();
            return { port, path: getSidecarPath() };
        } catch (err) {
            logWarn('refresh failed', { error: (err as Error).message });
            return { port: null, path: getSidecarPath() };
        }
    });

    // ── SUBSCRIBE ───────────────────────────────────────
    ipcMain.handle(IPC_CHANNELS.SUBSCRIBE, async (evt: IpcMainInvokeEvent) => {
        const wc = evt.sender;
        const id = wc.id;
        if (!subscribers.has(id)) {
            subscribers.set(id, { sender: wc });
            // Auto-cleanup when the window goes away.
            wc.once('destroyed', () => {
                subscribers.delete(id);
                if (subscribers.size === 0) {
                    try { watchDisposable?.dispose(); } catch { /* ignore */ }
                    watchDisposable = null;
                }
            });
            logInfo('subscriber registered', { id, count: subscribers.size });
        }
        ensureWatchArmed();
        // Send the current state immediately so the renderer doesn't
        // have to issue a second `get` round-trip after subscribing.
        try {
            const port = await getCurrentPort();
            wc.send(IPC_CHANNELS.CHANGED, { port, previous: null });
        } catch { /* ignore */ }
        return { subscribed: true };
    });

    // ── UNSUBSCRIBE ─────────────────────────────────────
    ipcMain.handle(IPC_CHANNELS.UNSUBSCRIBE, async (evt: IpcMainInvokeEvent) => {
        const id = evt.sender.id;
        const removed = subscribers.delete(id);
        if (subscribers.size === 0 && watchDisposable) {
            try { watchDisposable.dispose(); } catch { /* ignore */ }
            watchDisposable = null;
        }
        return { unsubscribed: removed };
    });

    logInfo('IPC handlers installed', {
        channels: Object.values(IPC_CHANNELS),
    });
}

/** Optional — dispose every handler & watcher. Used during app quit. */
export function uninstallPortSidecarHandlers(): void {
    for (const channel of Object.values(IPC_CHANNELS)) {
        try { ipcMain.removeHandler(channel); } catch { /* ignore */ }
    }
    subscribers.clear();
    try { watchDisposable?.dispose(); } catch { /* ignore */ }
    watchDisposable = null;
    try { portSidecar.dispose(); } catch { /* ignore */ }
}

/** Diagnostic — used by `/api/admin/diagnostics/connectivity` in tests. */
export function getSubscriberCount(): number {
    return subscribers.size;
}

/**
 * Manually push a change event to every subscriber. Used when the main
 * process detects a port change through a non-watcher path (e.g.
 * orchestrated server restart) and wants to fan-out immediately.
 */
export function broadcastChange(port: number | null, previous: number | null): void {
    for (const [id, entry] of subscribers) {
        const wc = entry.sender;
        if (wc.isDestroyed()) {
            subscribers.delete(id);
            continue;
        }
        try {
            wc.send(IPC_CHANNELS.CHANGED, { port, previous });
        } catch (err) {
            logWarn('manual broadcast failed', { id, error: (err as Error).message });
        }
    }
}

/**
 * Helper exposed for the main process — find every live WebContents
 * (used in some integration tests to assert subscription behaviour).
 */
export function listLiveWebContents(): number[] {
    return webContents.getAllWebContents().filter((w) => !w.isDestroyed()).map((w) => w.id);
}
