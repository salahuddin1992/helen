/**
 * Custom URL protocol handler — `commclient://` deep linking.
 *
 * Enables flows like:
 *    commclient://call/<call_id>
 *    commclient://chat/<user_id>
 *    commclient://channel/<channel_id>
 *    commclient://join?token=<invite_token>&server=<lan_url>
 *
 * Windows registers the scheme in HKCR at install time (see
 * build/installer.system.nsh) and at runtime via
 * `app.setAsDefaultProtocolClient`. Linux/macOS go through Electron's
 * own plist/desktop-file mechanism — we call the same API on all
 * platforms so NSIS failure doesn't leave us broken on dev machines.
 *
 * Deep links arrive in two shapes:
 *   1. Fresh launch with URL as argv[last] (Windows).
 *   2. `second-instance` / `open-url` on an already-running app.
 *
 * Both feed a shared dispatcher that emits IPC events to the focused
 * renderer. The renderer routes them via the existing react-router.
 */

import { app, BrowserWindow, ipcMain } from 'electron';

const SCHEME = 'commclient';

let pending: string | null = null;
let focusedWindow: () => BrowserWindow | null = () => null;

// Audit fix C7: Strict allowlist for commclient:// deep links so a
// malicious shortcut / browser link can't pivot the client to an
// attacker-controlled server. We accept ONLY:
//   - The action segment is one of a known set
//   - Optional `server=` query is checked against URL+host allowlist
//     (loopback / RFC1918 / *.local / configured serverUrl host)
//   - Total URL length capped at 2048 chars
const ALLOWED_ACTIONS = new Set([
  'call', 'chat', 'channel', 'join', 'user',
  'pair', 'open',
]);

function _isLanHost(host: string): boolean {
  if (!host) return false;
  if (host === 'localhost' || host === '127.0.0.1' || host === '[::1]') return true;
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  if (/^172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  if (/^169\.254\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  if (/\.local$/i.test(host)) return true;
  return false;
}

function extractUrl(argv: string[]): string | null {
  for (const a of argv) {
    if (typeof a !== 'string') continue;
    if (!a.startsWith(`${SCHEME}://`)) continue;
    if (a.length > 2048) {
      console.warn('[protocolHandler] rejected oversized URL:', a.length);
      continue;
    }
    // Action allowlist
    let parsed: URL;
    try { parsed = new URL(a); } catch { continue; }
    const action =
      parsed.hostname || parsed.pathname.split('/').filter(Boolean)[0] || '';
    if (!ALLOWED_ACTIONS.has(action.toLowerCase())) {
      console.warn('[protocolHandler] rejected unknown action:', action);
      continue;
    }
    // server= query pin: only accept if it points to a LAN-friendly host
    const server = parsed.searchParams.get('server');
    if (server) {
      try {
        const surl = new URL(server);
        if (
          (surl.protocol !== 'http:' && surl.protocol !== 'https:') ||
          !_isLanHost(surl.hostname)
        ) {
          console.warn('[protocolHandler] rejected server= pin:', server);
          continue;
        }
      } catch {
        console.warn('[protocolHandler] rejected malformed server= pin');
        continue;
      }
    }
    return a;
  }
  return null;
}

function parseDeepLink(url: string): {
  raw: string;
  action: string;      // "call" | "chat" | "channel" | "join" | "user" ...
  target: string;      // path component after action
  params: Record<string, string>;
} | null {
  try {
    // URL parsing — commclient://call/12345?x=1
    const parsed = new URL(url);
    if (parsed.protocol !== `${SCHEME}:`) return null;
    const action = parsed.hostname || parsed.pathname.split('/').filter(Boolean)[0] || '';
    const parts = parsed.pathname.split('/').filter(Boolean);
    const target = parsed.hostname ? parts.join('/') : parts.slice(1).join('/');
    const params: Record<string, string> = {};
    parsed.searchParams.forEach((v, k) => { params[k] = v; });
    return { raw: url, action, target, params };
  } catch {
    return null;
  }
}

function deliver(url: string | null): void {
  if (!url) return;
  const parsed = parseDeepLink(url);
  if (!parsed) {
    console.warn('[protocolHandler] malformed deep link:', url);
    return;
  }

  const win = focusedWindow();
  if (!win) {
    pending = url; // deliver on next window creation
    return;
  }

  console.log('[protocolHandler] deep link:', parsed.action, parsed.target);
  win.webContents.send('system:deep-link', parsed);

  // Bring the window to focus — deep links always imply user attention.
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// ─── public API ─────────────────────────────────────────────────────────

export interface ProtocolHandlerOptions {
  getMainWindow: () => BrowserWindow | null;
}

export function installProtocolHandler(opts: ProtocolHandlerOptions): void {
  focusedWindow = opts.getMainWindow;

  // Register the scheme as default. Windows requires exePath; Electron
  // handles that automatically when running packaged. For dev, we need
  // to pass the node/electron path explicitly so the registry points at
  // the right binary.
  if (process.defaultApp) {
    if (process.argv.length >= 2) {
      app.setAsDefaultProtocolClient(SCHEME, process.execPath, [
        process.argv[1],
      ]);
    }
  } else {
    app.setAsDefaultProtocolClient(SCHEME);
  }

  // Launch-time URL (Windows: argv; macOS: open-url after 'will-finish-launching')
  const initialUrl = extractUrl(process.argv);
  if (initialUrl) pending = initialUrl;

  app.on('will-finish-launching', () => {
    app.on('open-url', (event, url) => {
      event.preventDefault();
      deliver(url);
    });
  });

  // second-instance on Windows/Linux receives the argv from the attempted
  // new launch — that's how we get the deep link into the running app.
  app.on('second-instance', (_event, argv) => {
    const url = extractUrl(argv);
    if (url) deliver(url);
    else {
      const w = focusedWindow();
      if (w) {
        if (w.isMinimized()) w.restore();
        w.show();
        w.focus();
      }
    }
  });

  // Allow the renderer to ask "was I opened with a deep link?"
  ipcMain.handle('system:deep-link:get-initial', () => {
    const url = pending;
    pending = null;
    return url ? parseDeepLink(url) : null;
  });

  // Let the renderer request an update handler to flush pending URL now
  // that its listeners are attached. Called from useEffect on boot.
  ipcMain.handle('system:deep-link:flush', () => {
    if (pending) {
      deliver(pending);
      pending = null;
    }
  });
}

/** Programmatic deep-link delivery (used by other modules e.g. updater). */
export function dispatchDeepLink(url: string): void {
  deliver(url);
}
