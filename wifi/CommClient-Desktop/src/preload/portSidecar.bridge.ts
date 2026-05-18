/**
 * Port Sidecar Preload Bridge — Connectivity Hotfix Layer (Module A, preload).
 *
 * Exposes a minimal, context-isolated API to the renderer at
 * `window.helenPortSidecar`. The renderer's `portResolver.ts` uses it
 * to ask the main process for the canonical server URL, and to receive
 * change events when the bundled server's port rotates.
 *
 * Channel whitelist
 * -----------------
 * Only the IPC channels declared in `main/ipc/portSidecar.handlers.ts`
 * are reachable from this bridge. No arbitrary `ipcRenderer.send` is
 * exposed.
 */

import { contextBridge, ipcRenderer } from 'electron';

/** Channel names — must match `main/ipc/portSidecar.handlers.ts::IPC_CHANNELS`. */
const CH = {
    GET: 'port-sidecar:get',
    RESOLVE_URL: 'port-sidecar:resolve-url',
    SUBSCRIBE: 'port-sidecar:subscribe',
    UNSUBSCRIBE: 'port-sidecar:unsubscribe',
    CHANGED: 'port-sidecar:changed',
    REFRESH: 'port-sidecar:refresh',
} as const;

export interface PortSidecarGetResult {
    port: number | null;
    path: string;
    exists: boolean;
}

export interface PortSidecarResolveOptions {
    host?: string;
    protocol?: 'http' | 'https';
    fallbackPort?: number;
    noWait?: boolean;
}

export interface PortSidecarChangeEvent {
    port: number | null;
    previous: number | null;
}

export interface HelenPortSidecarBridge {
    /** One-shot probe — returns the current port + sidecar path + existence. */
    get(): Promise<PortSidecarGetResult>;
    /** Compose `${protocol}://${host}:${port}` using the resolved port. */
    resolveUrl(opts?: PortSidecarResolveOptions): Promise<string>;
    /** Force a fresh read of the sidecar file, bypassing the watcher's poll interval. */
    refresh(): Promise<{ port: number | null; path: string }>;
    /** Subscribe to port-change broadcasts. Returns an unsubscribe handle. */
    subscribe(cb: (payload: PortSidecarChangeEvent) => void): () => void;
}

const bridge: HelenPortSidecarBridge = {
    get: () => ipcRenderer.invoke(CH.GET),
    resolveUrl: (opts?: PortSidecarResolveOptions) =>
        ipcRenderer.invoke(CH.RESOLVE_URL, opts ?? {}),
    refresh: () => ipcRenderer.invoke(CH.REFRESH),
    subscribe: (cb: (payload: PortSidecarChangeEvent) => void) => {
        const handler = (_evt: Electron.IpcRendererEvent, payload: PortSidecarChangeEvent) => {
            try { cb(payload); } catch { /* renderer-side callback should never break IPC */ }
        };
        ipcRenderer.on(CH.CHANGED, handler);
        // Fire-and-forget subscribe — the main process registers our
        // WebContents id internally and starts emitting change events.
        ipcRenderer.invoke(CH.SUBSCRIBE).catch(() => { /* ignore */ });
        return () => {
            try { ipcRenderer.removeListener(CH.CHANGED, handler); } catch { /* ignore */ }
            ipcRenderer.invoke(CH.UNSUBSCRIBE).catch(() => { /* ignore */ });
        };
    },
};

/** Install the bridge. Idempotent — safe to call from `preload/index.ts`. */
export function exposePortSidecarBridge(): void {
    try {
        contextBridge.exposeInMainWorld('helenPortSidecar', bridge);
    } catch (err) {
        // Already exposed (HMR or re-run during dev) — non-fatal.
        // eslint-disable-next-line no-console
        console.warn('[portSidecar.bridge] expose failed:', (err as Error).message);
    }
}

// Auto-install on import so simply referencing this module from
// `preload/index.ts` is enough. The function is also exported for
// explicit invocation in tests.
exposePortSidecarBridge();

export default bridge;
