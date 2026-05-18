/**
 * Phase 3 / Module N — Custom protocol handler for desktop OAuth.
 *
 * Registers `helen://` as the default protocol client for this Electron
 * application and forwards any `helen://oauth/callback?code=…&state=…`
 * URL it intercepts to the active renderer window over IPC under the
 * channel name `'oauth:callback'`.
 *
 * Three integration points (Windows / macOS / Linux):
 *   - `app.setAsDefaultProtocolClient('helen')`  (called on first run).
 *   - `second-instance` event  — when a fresh launch with a deeplink
 *     reaches the existing process.
 *   - `open-url` event  — macOS-only delivery channel.
 *
 * The browser preload exposes:
 *   window.helenAPI.onOAuthCallback(cb) — subscribe to incoming callbacks.
 *
 * That preload must call `ipcRenderer.on('oauth:callback', …)` and
 * forward to its callbacks. See `preload/oauth.preload.ts` for that side.
 */

import { app, BrowserWindow, ipcMain } from 'electron';
import { URL } from 'url';

const PROTOCOL = 'helen';
const CALLBACK_CHANNEL = 'oauth:callback';

interface OAuthCallback {
  code?: string;
  state?: string;
  error?: string;
  raw: string;
}

let mainWindowGetter: () => BrowserWindow | null = () => null;
const pendingCallbacks: OAuthCallback[] = [];
let installed = false;

function parseHelenUrl(rawUrl: string): OAuthCallback | null {
  try {
    const u = new URL(rawUrl);
    if (u.protocol.replace(/:$/, '') !== PROTOCOL) return null;
    // Accept both `helen://oauth/callback?…` and `helen:oauth/callback?…`
    const host = (u.host || '').toLowerCase();
    const path = (u.pathname || '').toLowerCase();
    if (!(host === 'oauth' && path.startsWith('/callback'))
        && !(host === '' && path.startsWith('/oauth/callback'))) {
      return null;
    }
    const code = u.searchParams.get('code') || undefined;
    const state = u.searchParams.get('state') || undefined;
    const error = u.searchParams.get('error') || undefined;
    return { code, state, error, raw: rawUrl };
  } catch {
    return null;
  }
}

function dispatch(cb: OAuthCallback): void {
  const win = mainWindowGetter();
  if (win && !win.isDestroyed()) {
    try { win.webContents.send(CALLBACK_CHANNEL, cb); }
    catch { pendingCallbacks.push(cb); }
  } else {
    pendingCallbacks.push(cb);
  }
}

/**
 * Install the protocol handler. MUST be called early in the main process
 * lifecycle (before `app.whenReady()` returns).
 */
export function installOAuthProtocol(getMainWindow: () => BrowserWindow | null): void {
  if (installed) {
    mainWindowGetter = getMainWindow;
    return;
  }
  installed = true;
  mainWindowGetter = getMainWindow;

  // Register the protocol. On Windows / Linux, this writes a registry
  // entry / .desktop file pointing the scheme back at this binary.
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient(PROTOCOL, process.execPath, [process.argv[1]]);
  } else {
    app.setAsDefaultProtocolClient(PROTOCOL);
  }

  // Single-instance lock — without it, Windows spawns a NEW process for
  // each protocol invocation and our renderer never receives the callback.
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    app.quit();
    return;
  }

  // Windows / Linux delivery channel — the deeplink lands as a CLI arg
  // of a second-instance launch.
  app.on('second-instance', (_ev, argv) => {
    const win = mainWindowGetter();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
    const dl = argv.find(a => a.startsWith(`${PROTOCOL}://`));
    if (dl) {
      const cb = parseHelenUrl(dl);
      if (cb) dispatch(cb);
    }
  });

  // macOS delivery channel.
  app.on('open-url', (event, url) => {
    event.preventDefault();
    const cb = parseHelenUrl(url);
    if (cb) dispatch(cb);
  });

  // On first launch (already-cold-start with a deeplink in argv).
  app.whenReady().then(() => {
    const dl = process.argv.find(a => a.startsWith(`${PROTOCOL}://`));
    if (dl) {
      const cb = parseHelenUrl(dl);
      if (cb) dispatch(cb);
    }
    // Drain any callbacks the renderer wasn't ready for.
    flushPendingToFreshRenderer();
  });

  // Renderer pull-channel — the renderer can request the queue once it's
  // ready (e.g. after window.onload).
  ipcMain.handle('oauth:drain', () => {
    const out = pendingCallbacks.splice(0, pendingCallbacks.length);
    return out;
  });
}

function flushPendingToFreshRenderer(): void {
  const win = mainWindowGetter();
  if (!win || win.isDestroyed()) return;
  while (pendingCallbacks.length > 0) {
    const cb = pendingCallbacks.shift();
    if (cb) {
      try { win.webContents.send(CALLBACK_CHANNEL, cb); }
      catch { pendingCallbacks.unshift(cb); break; }
    }
  }
}

/** Test helper — inject a fake deeplink. */
export function _injectForTest(url: string): boolean {
  const cb = parseHelenUrl(url);
  if (cb) { dispatch(cb); return true; }
  return false;
}
