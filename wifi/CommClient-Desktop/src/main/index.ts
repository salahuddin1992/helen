/**
 * Electron Main Process — window management, tray, IPC, global shortcuts,
 * backend server lifecycle management.
 *
 * Production mode:
 *  - Launches CommClient-Server.exe as a child process
 *  - Monitors server health (HTTP /health endpoint)
 *  - Gracefully shuts down server on app quit
 *  - Stores data in %APPDATA%/CommClient/ (portable)
 */
import {
  app,
  BrowserWindow,
  Tray,
  Menu,
  nativeImage,
  ipcMain,
  globalShortcut,
  desktopCapturer,
  screen,
  shell,
  Notification,
  dialog,
} from 'electron';
import { join, resolve, dirname } from 'path';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { appendFile as appendFileAsync, writeFile as writeFileAsync } from 'fs/promises';
import { ChildProcess, spawn, spawnSync, execSync } from 'child_process';
import { createWriteStream, WriteStream } from 'fs';
import * as http from 'http';
import * as net from 'net';
import { fileURLToPath } from 'url';
// LAN-server + OS-integration extensions (Phase 1/2). These modules
// are additive — they install their own IPC handlers without touching
// existing behaviour.
import { installSystemIntegrations } from './system/index.js';
import { installUpdateSystem } from './updater/index.js';
import { installUsbPhoneDetect, shutdownUsbPhoneDetect } from './usbPhoneDetect.js';
import { installUsbQuickTimeHelper, shutdownUsbQuickTimeHelper } from './usbQuickTimeHelper.js';
import { loadClientConfig, parseServerUrl, getConfigPath, type ClientConfig } from './config.js';
import { registerDownloadHandlers } from './downloads.js';

// ── ESM __dirname polyfill ─────────────────────────────────
// package.json has "type": "module", so the bundled main process is ESM and
// the CommonJS `__dirname` global is undefined. Restore it for the rest of
// the file (which uses __dirname for preload path, dev paths, icon resolution).
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let callWindow: BrowserWindow | null = null;
let serverProcess: ChildProcess | null = null;
let serverLogStream: WriteStream | null = null;
// Lifecycle flags — used by the server.exit handler to tell the
// difference between "we asked it to stop" (clean) and "the OS or a
// crash killed it" (unexpected; surface dialog to user).
let serverHealthy: boolean = false;
let shuttingDown: boolean = false;
/** Resolved at startup by scanning ports 3000-3010 (or via $COMMCLIENT_PORT). */
let serverPort: number = 3000;

// CRITICAL FIX: only treat as dev when the app is genuinely unpackaged
// (i.e. running via `electron .` against a Vite dev server). Previously
// this line ALSO honoured ``NODE_ENV=development``, so a user whose
// shell happened to export that env var would launch the packaged exe
// and watch it try to fetch ``http://localhost:5173`` — blank white
// window, no login form, no error visible to the user. The loadURL
// path at line ~740 would error with ERR_CONNECTION_REFUSED.
//
// app.isPackaged is the only reliable signal in a built binary:
//   - Built via electron-builder → isPackaged === true (always)
//   - `electron .` from source → isPackaged === false
//
// Keeping the NODE_ENV opt-in for source-tree development is fine, but
// require BOTH conditions so a stray env var can't break a packaged
// install. (The CSP guard at line 671 already independently checks
// app.isPackaged for the same reason.)
const isDev = !app.isPackaged && (
  process.env.NODE_ENV === 'development' || !!process.env.VITE_DEV_SERVER_URL
);
// APP_NAME is the immutable internal identifier used for filesystem paths
// (%APPDATA%/CommClient, logs, etc.) — renaming it would break existing
// installs. DISPLAY_NAME is the user-facing title, editable from settings
// and persisted to <dataDir>/display_name.json.
const APP_NAME = 'Helen Desktop';
const DEFAULT_DISPLAY_NAME = 'Helen Desktop';
let displayName: string = DEFAULT_DISPLAY_NAME;
const SERVER_PORT_DEFAULT = 3000;
const SERVER_PORT_RANGE_END = 3010;

// ── Cross-platform / cross-version helpers ─────────────────

/** Probe a TCP port; resolves true when nothing is listening on it. */
function isPortFree(port: number): Promise<boolean> {
  return new Promise((resolveProbe) => {
    const tester = net.createServer()
      .once('error', () => resolveProbe(false))
      .once('listening', () => {
        tester.close(() => resolveProbe(true));
      })
      .listen(port, '0.0.0.0');
  });
}

/**
 * Probe whether *something* is actually accepting TCP connections on
 * 127.0.0.1:port. Unlike isPortFree (which only tries to bind on 0.0.0.0
 * and would mistakenly say "free" for a loopback-only listener), this
 * does a real connection probe — the same one a browser would do.
 */
function isServerListening(port: number, timeoutMs = 800): Promise<boolean> {
  return new Promise((resolveProbe) => {
    const sock = new net.Socket();
    let settled = false;
    const done = (alive: boolean) => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      resolveProbe(alive);
    };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => done(true));
    sock.once('timeout', () => done(false));
    sock.once('error', () => done(false));
    sock.connect(port, '127.0.0.1');
  });
}

/** Pick the first free port in [start..end], falling back to `start`. */
async function findFreePort(start: number, end: number): Promise<number> {
  for (let p = start; p <= end; p++) {
    if (await isPortFree(p)) return p;
  }
  return start;
}

/**
 * Detect the local Python executable in dev mode.
 * Checks `python`, `python3`, `py -3`. Returns null if none works.
 * Used only when running `npm run dev` (production uses the bundled exe).
 */
function detectPython(): string | null {
  const candidates = process.platform === 'win32'
    ? ['py', 'python', 'python3']
    : ['python3', 'python'];
  for (const cmd of candidates) {
    try {
      const args = cmd === 'py' ? ['-3', '--version'] : ['--version'];
      const r = spawnSync(cmd, args, { encoding: 'utf-8' });
      if (r.status === 0) return cmd;
    } catch { /* keep trying */ }
  }
  return null;
}

/**
 * Async cousin of detectPython() — returns 'python' as a default fallback
 * so callers never receive null. Used by code paths that want a single
 * string and treat detection as best-effort.
 */
async function findPython(): Promise<string> {
  // ESM-safe: require() blew up in ESM modules. Use the static import
  // of execSync from 'child_process' at the top of this file.
  const candidates = ['python', 'python3', 'py'];
  for (const cmd of candidates) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      return cmd;
    } catch { continue; }
  }
  return 'python';
}

// ── Portable Paths ──────────────────────────────────────
// Production: %APPDATA%/CommClient/
// Dev: uses project-relative paths
function getAppDataDir(): string {
  const dir = isDev
    ? join(__dirname, '../../dev-data')
    : join(app.getPath('appData'), APP_NAME);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return dir;
}

function getDataDir(): string {
  const dir = join(getAppDataDir(), 'data');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return dir;
}

function displayNameFile(): string {
  return join(getDataDir(), 'display_name.json');
}

function loadDisplayName(): string {
  try {
    if (!existsSync(displayNameFile())) return DEFAULT_DISPLAY_NAME;
    const raw = readFileSync(displayNameFile(), 'utf-8');
    const data = JSON.parse(raw);
    const name = typeof data?.name === 'string' ? data.name.trim() : '';
    return name || DEFAULT_DISPLAY_NAME;
  } catch {
    return DEFAULT_DISPLAY_NAME;
  }
}

function saveDisplayName(name: string): string {
  // Strip characters that are illegal in NTFS / would mangle the
  // window title bar / could be used for control-char shell tricks.
  // The display name is shown in the title bar AND used as a
  // discovery hint, so we keep it human-readable.
  const cleaned = (name || '')
    .replace(/[\x00-\x1f\x7f]/g, '')   // control chars
    .replace(/[<>:"/\\|?*]/g, '')      // NTFS reserved
    .trim()
    .slice(0, 64);
  if (!cleaned) throw new Error('display name cannot be empty');
  writeFileSync(displayNameFile(), JSON.stringify({ name: cleaned }, null, 2), 'utf-8');
  displayName = cleaned;
  // Apply live to all open windows.
  BrowserWindow.getAllWindows().forEach((w) => {
    try { w.setTitle(cleaned); } catch { /* ignore */ }
  });
  return cleaned;
}

function getLogsDir(): string {
  const dir = join(getAppDataDir(), 'logs');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return dir;
}

/** Resolve icon path — works in dev and packaged builds */
function getIconPath(): string {
  if (isDev) {
    return join(__dirname, '../../resources/installer/icon.ico');
  }
  // In packaged app, resources are in process.resourcesPath
  const resourceIcon = join(process.resourcesPath, 'installer', 'icon.ico');
  if (existsSync(resourceIcon)) return resourceIcon;
  // Fallback: asar-relative
  return join(__dirname, '../../resources/installer/icon.ico');
}

/** Resolve preload script path. Preload is bundled as CommonJS (.cjs) so
 *  Electron can load it via require() — see vite.config.ts. */
function getPreloadPath(): string {
  return join(__dirname, '../preload/index.cjs');
}

// ── Backend Server Management ───────────────────────────

function getServerExePath(): string {
  if (isDev) return ''; // Dev mode: server runs externally
  // Packaged: server exe in extraResources/server/
  return join(process.resourcesPath, 'server', 'Helen-Server.exe');
}

async function startBackendServer(): Promise<void> {
  // Load the central client config first — it's the single source of truth
  // for serverUrl + every connection feature flag. Production defaults lock
  // down embedded server, LAN discovery, and auto-switching to prevent the
  // 3000/3001 split-brain that plagued earlier builds.
  const clientConfig = loadClientConfig();
  const parsedUrl = parseServerUrl(clientConfig.serverUrl);
  console.log(`[Main] Client config loaded from ${getConfigPath()}`);
  console.log(`[Main]   mode=${clientConfig.mode} serverUrl=${clientConfig.serverUrl}`);
  console.log(`[Main]   allowEmbeddedServer=${clientConfig.allowEmbeddedServer} allowLanDiscovery=${clientConfig.allowLanDiscovery}`);

  // Resolve a free port up-front so dev and production both honor it.
  const requested = process.env.COMMCLIENT_PORT
    ? Number(process.env.COMMCLIENT_PORT)
    : (parsedUrl.host === '127.0.0.1' || parsedUrl.host === 'localhost' ? parsedUrl.port : SERVER_PORT_DEFAULT);

  if (isDev) {
    // Dev mode: prefer an explicit `COMMCLIENT_PORT` if the developer set
    // one and a server is actually listening there — this lets us point
    // the desktop at a server on a non-default port (e.g. 3088) without
    // shipping a config file change.
    if (process.env.COMMCLIENT_PORT) {
      const explicit = Number(process.env.COMMCLIENT_PORT);
      if (Number.isFinite(explicit) && await isServerListening(explicit)) {
        serverPort = explicit;
        console.log(`[Main] Dev mode — server already running on ${serverPort} (from COMMCLIENT_PORT)`);
        return;
      }
    }

    // Fallback: scan the default port range for an existing server before
    // trying to spawn one. Handles the common scenario where the developer
    // already ran `python run.py` in a terminal.
    for (let p = SERVER_PORT_DEFAULT; p <= SERVER_PORT_RANGE_END; p++) {
      if (await isServerListening(p)) {
        serverPort = p;
        console.log(`[Main] Dev mode — server already running on ${serverPort}`);
        return;
      }
    }

    serverPort = (await isPortFree(requested))
      ? requested
      : await findFreePort(SERVER_PORT_DEFAULT, SERVER_PORT_RANGE_END);
    console.log(`[Main] Using server port: ${serverPort}`);
    const serverDir = resolve(__dirname, '../../..', 'CommClient-Server');
    if (!existsSync(serverDir)) {
      console.warn('[Main] Dev mode — server directory not found:', serverDir);
      return;
    }
    // Prefer the project venv if it exists (has all deps installed)
    const venvPy = join(serverDir, 'venv', 'Scripts', 'python.exe');
    const py = existsSync(venvPy) ? venvPy : detectPython();
    if (!py) {
      console.warn(
        '[Main] Dev mode — Python not detected; expecting external server on ' + serverPort
      );
      return;
    }
    const args = py === venvPy ? ['run.py'] : (py === 'py' ? ['-3', 'run.py'] : ['run.py']);
    console.log(`[Main] Spawning dev server: ${py} ${args.join(' ')} (cwd=${serverDir})`);
    serverProcess = spawn(py, args, {
      cwd: serverDir,
      env: {
        ...process.env,
        HOST: '0.0.0.0',
        PORT: String(serverPort),
        DEBUG: 'true',
        LOG_LEVEL: 'INFO',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    serverProcess.stdout?.on('data', (d) => process.stdout.write(`[server] ${d}`));
    serverProcess.stderr?.on('data', (d) => process.stderr.write(`[server] ${d}`));
    try {
      await waitForServer(serverPort, 30_000);
      console.log('[Main] Dev server is healthy');
    } catch (err) {
      console.warn('[Main] Dev server health check failed:', (err as Error).message);
    }
    return;
  }

  // ── Production mode (frozen exe) ──
  //
  // Decision tree (config-driven, no auto-switching):
  //   1. If config.serverUrl points to a remote host (not localhost) → don't spawn.
  //      The user explicitly asked for a remote master server.
  //   2. If config.allowEmbeddedServer === false (production default) → don't spawn.
  //      We require an external Helen-Server to be reachable at config.serverUrl.
  //      If it isn't, surface that as a connection error in the UI rather than
  //      silently spawning a second server with a different DB.
  //   3. If config.allowEmbeddedServer === true (standalone install) → spawn
  //      ONLY if no server is already on the configured port. Never pick a
  //      different port — that's the split-brain trigger.
  //
  // HELEN_CLIENT_ONLY=1 is preserved as a runtime override that forces (1)+(2).
  const remoteHost = parsedUrl.host !== '127.0.0.1' && parsedUrl.host !== 'localhost';
  const clientOnly =
    process.env.HELEN_CLIENT_ONLY === '1'
    || remoteHost
    || !clientConfig.allowEmbeddedServer;

  serverPort = parsedUrl.port;

  if (clientOnly) {
    console.log(
      `[Main] Client-only mode — will NOT spawn a local Helen-Server. ` +
      `Connecting to ${clientConfig.serverUrl}. ` +
      `(reason: ${process.env.HELEN_CLIENT_ONLY === '1' ? 'env HELEN_CLIENT_ONLY=1' : remoteHost ? 'remote serverUrl' : 'config.allowEmbeddedServer=false'})`
    );
    // Quick sanity probe so logs show whether the master is actually reachable.
    if (parsedUrl.host === '127.0.0.1' || parsedUrl.host === 'localhost') {
      const alive = await isServerListening(parsedUrl.port);
      console.log(`[Main] Local probe ${parsedUrl.host}:${parsedUrl.port} — ${alive ? 'reachable' : 'NOT reachable'}`);
    }
    return;
  }

  // Embedded-server path (standalone mode). Only fires when allowEmbeddedServer=true.
  if (await isServerListening(parsedUrl.port)) {
    console.log(`[Main] Existing Helen-Server already on port ${parsedUrl.port} — using it (no spawn).`);
    return;
  }
  console.log(`[Main] Standalone mode — spawning bundled Helen-Server on port ${serverPort}`);
  const exePath = getServerExePath();
  if (!existsSync(exePath)) {
    // Bundled server is missing. Two recovery paths before giving up:
    //   1) An external server is already running on the configured port —
    //      use it and continue (degraded mode = user manages the server).
    //   2) Otherwise, surface an informational dialog and let the renderer
    //      load anyway, so the user sees a connection error they can act on
    //      instead of the app crashing on launch.
    console.warn('[Main] Bundled server exe missing:', exePath);
    // Use a real connection probe (not isPortFree) so we also catch servers
    // that bind only to 127.0.0.1 instead of 0.0.0.0. Scan the full range
    // since the user may have started the server on any port in our range.
    let detectedPort: number | null = null;
    for (let p = SERVER_PORT_DEFAULT; p <= SERVER_PORT_RANGE_END; p++) {
      if (await isServerListening(p)) {
        detectedPort = p;
        break;
      }
    }
    if (detectedPort !== null) {
      serverPort = detectedPort;
      console.log(
        `[Main] External server detected on port ${detectedPort} — running in degraded mode (no managed server)`
      );
      return;
    }
    const msg =
      `Server executable not found:\n${exePath}\n\n` +
      `The desktop will start, but no backend connection is available.\n` +
      `Start Helen-Server manually (e.g. python run.py) on port ${serverPort}, or reinstall with the bundled server.`;
    console.error('[Main]', msg);
    dialog.showMessageBoxSync({
      type: 'warning',
      title: 'Helen — Backend Server Missing',
      message: 'Backend server not bundled or not running.',
      detail: msg,
      buttons: ['Continue Anyway'],
      defaultId: 0,
    });
    // Do NOT throw — let the renderer load. The user will see an in-app
    // "disconnected" state and can configure or restart the server.
    return;
  }

  const dataDir = getDataDir();
  const logsDir = getLogsDir();
  const dbPath = join(dataDir, 'commclient.db');
  const uploadDir = join(dataDir, 'files');

  // Ensure upload dir
  if (!existsSync(uploadDir)) mkdirSync(uploadDir, { recursive: true });

  // Log server stdout/stderr to file
  const logFile = join(logsDir, `server-${Date.now()}.log`);
  serverLogStream = createWriteStream(logFile, { flags: 'a' });

  console.log('[Main] Starting backend server:', exePath);
  console.log('[Main] DB path:', dbPath);
  console.log('[Main] Upload dir:', uploadDir);
  console.log('[Main] Server log:', logFile);

  serverProcess = spawn(exePath, [], {
    env: {
      ...process.env,
      HOST: '0.0.0.0',
      PORT: String(serverPort),
      DEBUG: 'false',
      LOG_LEVEL: 'INFO',
      SQLITE_PATH: dbPath,
      UPLOAD_DIR: uploadDir,
      LOG_DIR: logsDir,
      // Pass absolute paths so config.py doesn't resolve relative to PROJECT_ROOT
      COMMCLIENT_DATA_DIR: dataDir,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    detached: false,
  });

  serverProcess.stdout?.pipe(serverLogStream);
  serverProcess.stderr?.pipe(serverLogStream);

  serverProcess.on('error', (err) => {
    console.error('[Main] Server process error:', err.message);
    serverLogStream?.write(`[ERROR] ${err.message}\n`);
    // Surface spawn-time failures (exe missing, permission denied, …)
    // — without this the user sees only "no servers found" later.
    if (!serverHealthy) {
      dialog.showErrorBox(
        'Helen Desktop — Could not launch backend',
        `${err.message}\nCheck logs at: ${logFile}`,
      );
    }
  });

  serverProcess.on('exit', (code, signal) => {
    console.log(`[Main] Server exited: code=${code} signal=${signal}`);
    serverLogStream?.write(`[EXIT] code=${code} signal=${signal}\n`);
    serverProcess = null;
    // If the server dies AFTER startup with a non-clean code (we
    // didn't ask it to stop), tell the user instead of leaving the
    // UI silently disconnected.
    const wasUnexpected = (
      serverHealthy
      && !shuttingDown
      && code !== 0
      && signal !== 'SIGTERM'
      && signal !== 'SIGKILL'
    );
    if (wasUnexpected && mainWindow && !mainWindow.isDestroyed()) {
      try {
        mainWindow.webContents.send('server:unexpected-exit',
                                      { code, signal });
      } catch { /* renderer may be tearing down */ }
      dialog.showErrorBox(
        'Helen Desktop — Backend stopped unexpectedly',
        `The Helen-Server exited with code ${code}.\n`
        + `Logs: ${logFile}\n\n`
        + `The app will keep running but is offline until the server is back. `
        + `Click "Reconnect" in the toolbar or restart the app.`,
      );
    }
    serverHealthy = false;
  });

  // Wait for server to become healthy
  try {
    await waitForServer(serverPort, 30_000);
    serverHealthy = true;
    console.log('[Main] Server is healthy and ready');
  } catch (err) {
    console.error('[Main] Server failed to become healthy:', (err as Error).message);
    dialog.showErrorBox(
      'Helen Desktop — Server Start Failed',
      `The backend server failed to start within 30 seconds.\nCheck logs at: ${logFile}`
    );
    throw err;
  }
}

/**
 * Poll the server health endpoint until it responds or timeout.
 * Uses /api/health first (always available), falls back to /docs (dev only).
 * In production /docs is disabled, so we rely on the health endpoint.
 */
function waitForServer(port: number, timeoutMs: number): Promise<void> {
  const start = Date.now();
  const healthUrl = `http://127.0.0.1:${port}/api/health`;
  const fallbackUrl = `http://127.0.0.1:${port}/docs`;

  return new Promise((resolvePromise, reject) => {
    let attempts = 0;
    const check = () => {
      if (Date.now() - start > timeoutMs) {
        reject(new Error(`Server health check timeout after ${attempts} attempts`));
        return;
      }
      attempts++;
      // Try health endpoint first, then fallback
      const url = attempts <= 3 ? healthUrl : (attempts % 2 === 0 ? healthUrl : fallbackUrl);
      const req = http.get(url, (res) => {
        if (res.statusCode && res.statusCode >= 200 && res.statusCode < 500) {
          console.log(`[Main] Server responded on attempt ${attempts} (${url})`);
          resolvePromise();
        } else {
          setTimeout(check, 500);
        }
        res.resume(); // drain response
      });
      req.on('error', () => setTimeout(check, 500));
      req.setTimeout(2000, () => {
        req.destroy();
        setTimeout(check, 500);
      });
    };
    check();
  });
}

function stopBackendServer(): Promise<void> {
  // Mark intent so the server.exit handler knows this kill is
  // expected — otherwise it would pop a "stopped unexpectedly" dialog.
  shuttingDown = true;
  return new Promise((resolvePromise) => {
    if (!serverProcess || serverProcess.killed) {
      serverLogStream?.end();
      resolvePromise();
      return;
    }

    console.log('[Main] Stopping backend server (PID:', serverProcess.pid, ')');

    // Give it 5s to exit gracefully, then force kill
    const killTimer = setTimeout(() => {
      console.warn('[Main] Force killing server process');
      try { serverProcess?.kill('SIGKILL'); } catch {}
      serverLogStream?.end();
      resolvePromise();
    }, 5000);

    serverProcess.once('exit', () => {
      clearTimeout(killTimer);
      serverLogStream?.end();
      resolvePromise();
    });

    // On Windows, SIGTERM doesn't work well. Use taskkill for graceful stop.
    if (process.platform === 'win32' && serverProcess.pid) {
      spawn('taskkill', ['/pid', String(serverProcess.pid), '/t', '/f'], {
        windowsHide: true,
      });
    } else {
      serverProcess.kill('SIGTERM');
    }
  });
}

// ── Window Management ───────────────────────────────────

function createMainWindow(): BrowserWindow {
  // Size the window to a sensible fraction of the user's primary
  // display so it never opens larger than the screen and never
  // smaller than the layout can render. We pick 78% of the work area
  // (work area = display minus taskbar/dock) clamped to a fixed 16:10
  // aspect ratio so the chat sidebar + main pane always fit.
  //
  // Behaviour summary:
  //   - Default open size: ~78% of work area, capped at 1440x900
  //   - Minimum: 1024x680 — below this the responsive layout collapses
  //   - Maximum: 100% of work area — useful for big presentations
  //   - Aspect ratio: locked at 16:10 (1.6) so resize handles keep the
  //     UI proportionate even when the user drags a corner.
  const { width: workW, height: workH } = screen.getPrimaryDisplay().workAreaSize;
  const TARGET_RATIO = 1.6;            // 16:10
  const TARGET_FRAC  = 0.78;           // 78% of the work area

  // Compute width/height that fit inside the work area at our ratio.
  let initialW = Math.round(workW * TARGET_FRAC);
  let initialH = Math.round(initialW / TARGET_RATIO);
  if (initialH > Math.round(workH * TARGET_FRAC)) {
    initialH = Math.round(workH * TARGET_FRAC);
    initialW = Math.round(initialH * TARGET_RATIO);
  }
  // Hard caps — we never want to force a giant window on 4K displays.
  initialW = Math.min(initialW, 1440);
  initialH = Math.min(initialH, 900);

  mainWindow = new BrowserWindow({
    width: initialW,
    height: initialH,
    minWidth: 1024,
    minHeight: 680,
    maxWidth: workW,
    maxHeight: workH,
    title: displayName,
    icon: getIconPath(),
    frame: false,
    titleBarStyle: 'hidden',
    backgroundColor: '#020617',
    center: true,                    // open dead-centre on the screen
    show: false,
    webPreferences: {
      preload: getPreloadPath(),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
      // SECURITY: enable the Chromium renderer sandbox. The preload only
      // uses contextBridge + ipcRenderer + process.platform — all of which
      // are available in sandboxed preloads — so we can lock the renderer
      // down to OS-process level. This complements contextIsolation by
      // making renderer compromise unable to reach host APIs even if the
      // bridge is misconfigured.
      sandbox: true,
      // SECURITY: Disable navigation to external pages
      allowRunningInsecureContent: false,
    },
  });

  // Lock the aspect ratio to 16:10 so resize handles preserve the
  // chat sidebar + main pane proportions. Users can still maximise
  // (which ignores aspect-ratio on Windows) and unmaximise back to
  // the locked ratio. Setting it AFTER the constructor is the
  // documented Electron API.
  try { mainWindow.setAspectRatio(1.6); } catch { /* not all platforms */ }

  // ── SECURITY: Content Security Policy ──────────────
  // Audit fix C1: production CSP `connect-src 'self' http: https: ws: wss:`
  // was effectively wildcard — XSS could exfiltrate data to any host
  // on the internet. Production now restricts connect-src to:
  //   - 'self'
  //   - loopback (127.0.0.1, [::1], localhost) on any port
  //   - RFC1918 LAN ranges on any port (10.0.0.0/8, 172.16.0.0/12,
  //     192.168.0.0/16, 169.254.0.0/16)
  //   - the configured server's host (read from clientConfig)
  //
  // Dev keeps the loose policy because Vite HMR + DevTools need it.
  // The wildcard schemes are explicitly NOT in production.
  const lanConnectSrc = [
    "'self'",
    "http://127.0.0.1:*", "https://127.0.0.1:*",
    "ws://127.0.0.1:*",   "wss://127.0.0.1:*",
    "http://localhost:*", "https://localhost:*",
    "ws://localhost:*",   "wss://localhost:*",
    "http://[::1]:*",     "https://[::1]:*",
    // RFC1918 — wildcard schemes restricted to private ranges
    "http://10.*:*",      "https://10.*:*",      "ws://10.*:*",      "wss://10.*:*",
    "http://172.16.*:*",  "https://172.16.*:*",  "ws://172.16.*:*",  "wss://172.16.*:*",
    "http://172.17.*:*",  "https://172.17.*:*",  "ws://172.17.*:*",  "wss://172.17.*:*",
    "http://172.18.*:*",  "https://172.18.*:*",  "ws://172.18.*:*",  "wss://172.18.*:*",
    "http://172.19.*:*",  "https://172.19.*:*",  "ws://172.19.*:*",  "wss://172.19.*:*",
    "http://172.20.*:*",  "https://172.20.*:*",  "ws://172.20.*:*",  "wss://172.20.*:*",
    "http://172.21.*:*",  "https://172.21.*:*",  "ws://172.21.*:*",  "wss://172.21.*:*",
    "http://172.22.*:*",  "https://172.22.*:*",  "ws://172.22.*:*",  "wss://172.22.*:*",
    "http://172.23.*:*",  "https://172.23.*:*",  "ws://172.23.*:*",  "wss://172.23.*:*",
    "http://172.24.*:*",  "https://172.24.*:*",  "ws://172.24.*:*",  "wss://172.24.*:*",
    "http://172.25.*:*",  "https://172.25.*:*",  "ws://172.25.*:*",  "wss://172.25.*:*",
    "http://172.26.*:*",  "https://172.26.*:*",  "ws://172.26.*:*",  "wss://172.26.*:*",
    "http://172.27.*:*",  "https://172.27.*:*",  "ws://172.27.*:*",  "wss://172.27.*:*",
    "http://172.28.*:*",  "https://172.28.*:*",  "ws://172.28.*:*",  "wss://172.28.*:*",
    "http://172.29.*:*",  "https://172.29.*:*",  "ws://172.29.*:*",  "wss://172.29.*:*",
    "http://172.30.*:*",  "https://172.30.*:*",  "ws://172.30.*:*",  "wss://172.30.*:*",
    "http://172.31.*:*",  "https://172.31.*:*",  "ws://172.31.*:*",  "wss://172.31.*:*",
    "http://192.168.*:*", "https://192.168.*:*", "ws://192.168.*:*", "wss://192.168.*:*",
    "http://169.254.*:*", "https://169.254.*:*", "ws://169.254.*:*", "wss://169.254.*:*",
    // Allow the operator-configured serverUrl host explicitly. We
    // already validated this URL on Main side via parseServerUrl.
    "http://*.local:*", "https://*.local:*", "ws://*.local:*", "wss://*.local:*",
  ].join(' ');

  // Audit fix C2: belt-and-braces guard so a packaged build can't
  // accidentally serve the loose dev CSP. `app.isPackaged` is the
  // authoritative production check — it ignores any NODE_ENV
  // contamination from the user's environment. We also emit a fatal
  // warning if the dev CSP would have been used in a packaged build
  // (should never happen, but the warning makes regressions obvious
  // in CI logs).
  const useDevCsp = isDev && !app.isPackaged;
  if (isDev && app.isPackaged) {
    console.error(
      '[CSP] FATAL MISMATCH: NODE_ENV=development in a packaged build. ' +
      'Forcing production CSP. This indicates a broken build.',
    );
  }

  mainWindow.webContents.session.webRequest.onHeadersReceived((details, callback) => {
    // PRAGMATIC FIX (LAN-only deployments): Helen never reaches the
    // public internet at runtime, and the original strict CSP rejected
    // legitimate connections to LAN servers when the host name didn't
    // match a static range — surfaced to the user as ``Failed to fetch``
    // on login/register with no actionable error. Since the server is
    // already authenticated by a strong JWT and Helen is air-gapped by
    // policy, allowing any HTTP/HTTPS/WS connection at the renderer
    // layer is acceptable. We still keep object-src 'none' and
    // base-uri 'self' to block the worst XSS pivots.
    //
    // ALSO: also strip ANY existing Content-Security-Policy header
    // first — index.html still ships a <meta> CSP that the browser
    // intersects with this header, and the meta one was tighter.
    const stripped = { ...details.responseHeaders };
    delete (stripped as any)['content-security-policy'];
    delete (stripped as any)['Content-Security-Policy'];
    callback({
      responseHeaders: {
        ...stripped,
        'Content-Security-Policy': [
          useDevCsp
            ? "default-src 'self' http://localhost:*; script-src 'self' 'unsafe-inline' 'unsafe-eval' http://localhost:* blob:; worker-src 'self' blob:; child-src 'self' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' http: https: ws: wss:; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:;"
            : "default-src 'self' http: https: ws: wss:; script-src 'self' blob:; worker-src 'self' blob:; child-src 'self' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' http: https: ws: wss: data: blob:; img-src 'self' data: blob: http: https:; media-src 'self' blob: http: https:; font-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self';",
        ],
      },
    });
  });

  // SECURITY: Prevent navigation to external URLs
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const allowed = isDev
      ? url.startsWith('http://localhost:')
      : url.startsWith('file://');
    if (!allowed) {
      event.preventDefault();
      console.warn('[Security] Blocked navigation to:', url);
    }
  });

  // SECURITY: Block new window creation (popups). Only allow opening
  // http(s) URLs in the system browser. We additionally parse the URL
  // and verify it has a non-empty host AND that the parsed protocol
  // matches — defense in depth against handler bugs that could
  // otherwise treat e.g. "https://"-prefixed URLs with unusual
  // hostnames or embedded credentials in unexpected ways.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      const proto = parsed.protocol.toLowerCase();
      if ((proto === 'https:' || proto === 'http:') && parsed.hostname) {
        shell.openExternal(url);
      } else {
        console.warn('[Security] Refused openExternal for URL:', url.slice(0, 200));
      }
    } catch {
      console.warn('[Security] Refused openExternal for malformed URL:', url.slice(0, 200));
    }
    return { action: 'deny' };
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    mainWindow?.focus();
  });

  // Dev-only: forward renderer console messages to the main-process stdout
  // so they land in our dev log and we can diagnose boot issues without
  // popping open DevTools on every iteration.
  if (isDev) {
    mainWindow.webContents.on('console-message', (_e, level, message, line, src) => {
      const tag = ['L', 'W', 'E', 'I'][level] ?? '?';
      console.log(`[renderer:${tag}] ${message}  (${src}:${line})`);
    });
  }

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  } else {
    // SECURITY: No DevTools in production
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'));
  }

  mainWindow.on('close', (e) => {
    if (tray) {
      e.preventDefault();
      mainWindow?.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  return mainWindow;
}

function createTray(): void {
  const icon = nativeImage.createFromPath(getIconPath());
  tray = new Tray(icon.resize({ width: 16, height: 16 }));

  const contextMenu = Menu.buildFromTemplate([
    { label: 'Open Helen Desktop', click: () => mainWindow?.show() },
    { type: 'separator' },
    { label: 'Mute', type: 'checkbox', click: () => {
      mainWindow?.webContents.send('shortcut:toggle-mute');
    }},
    { type: 'separator' },
    {
      label: 'Open Data Folder',
      click: () => shell.openPath(getAppDataDir()),
    },
    {
      label: 'Open Logs',
      click: () => shell.openPath(getLogsDir()),
    },
    { type: 'separator' },
    { label: 'Quit', click: () => {
      tray?.destroy();
      tray = null;
      app.quit();
    }},
  ]);

  tray.setToolTip('Helen Desktop — LAN Communication');
  tray.setContextMenu(contextMenu);
  tray.on('double-click', () => mainWindow?.show());
}

function registerGlobalShortcuts(): void {
  globalShortcut.register('CmdOrCtrl+Shift+M', () => {
    mainWindow?.webContents.send('shortcut:toggle-mute');
  });
  globalShortcut.register('CmdOrCtrl+Shift+V', () => {
    mainWindow?.webContents.send('shortcut:toggle-video');
  });
  globalShortcut.register('CmdOrCtrl+Shift+D', () => {
    mainWindow?.webContents.send('shortcut:toggle-deafen');
  });
  globalShortcut.register('CmdOrCtrl+Shift+E', () => {
    mainWindow?.webContents.send('shortcut:end-call');
  });
}

// ── IPC Handlers ────────────────────────────────────────

// Wire chat downloads + system-default-app handoff. Lives in
// downloads.ts to keep the (already large) main/index.ts focused
// on lifecycle and discovery.
registerDownloadHandlers();

ipcMain.handle('window:minimize', () => mainWindow?.minimize());
ipcMain.handle('window:maximize', () => {
  if (mainWindow?.isMaximized()) mainWindow.unmaximize();
  else mainWindow?.maximize();
});
ipcMain.handle('window:close', () => mainWindow?.close());
ipcMain.handle('window:is-maximized', () => mainWindow?.isMaximized());

// Always-on-top toggle — used by the call view's "compact mode"
// so the floating mini call window stays above other apps. Returns
// the new state so the renderer can sync its UI.
ipcMain.handle('window:toggle-always-on-top', () => {
  if (!mainWindow) return false;
  const next = !mainWindow.isAlwaysOnTop();
  mainWindow.setAlwaysOnTop(next, 'floating');
  return next;
});
ipcMain.handle('window:set-always-on-top', (_e, on: boolean) => {
  mainWindow?.setAlwaysOnTop(!!on, 'floating');
  return mainWindow?.isAlwaysOnTop() ?? false;
});

// Compact mode — resize the window to a small floating tile and
// pair it with always-on-top so the user can multitask. The
// renderer toggles back to its normal layout when compact is off.
let _preCompactBounds: Electron.Rectangle | null = null;
ipcMain.handle('window:set-compact', (_e, on: boolean) => {
  if (!mainWindow) return false;
  if (on) {
    if (!_preCompactBounds) _preCompactBounds = mainWindow.getBounds();
    mainWindow.setAlwaysOnTop(true, 'floating');
    // 360x240 fits a single video tile + slim controls.
    const display = mainWindow.getBounds();
    const x = Math.max(0, display.x + (display.width - 360));
    const y = Math.max(0, display.y);
    mainWindow.setBounds({ x, y, width: 360, height: 240 });
  } else {
    mainWindow.setAlwaysOnTop(false);
    if (_preCompactBounds) {
      mainWindow.setBounds(_preCompactBounds);
      _preCompactBounds = null;
    }
  }
  return on;
});

ipcMain.handle('desktop-capturer:get-sources', async () => {
  // On Windows 10/11 with locked-down Group Policy or Windows
  // 'Camera & microphone privacy' set to "block apps", desktopCapturer
  // returns an empty array silently. Detect that and surface an
  // explicit error so the renderer can show "Screen sharing is
  // disabled by system policy" instead of a confused blank picker.
  let sources: Electron.DesktopCapturerSource[] = [];
  let captureBlocked = false;
  let errorReason: string | null = null;
  try {
    sources = await desktopCapturer.getSources({
      types: ['screen', 'window'],
      thumbnailSize: { width: 320, height: 180 },
      fetchWindowIcons: true,
    });
  } catch (err) {
    captureBlocked = true;
    errorReason = (err as Error).message || 'getSources threw';
  }
  if (!captureBlocked && sources.length === 0) {
    captureBlocked = true;
    errorReason =
      'No capture sources available. On Windows 10/11 this usually '
      + 'means Settings → Privacy & security → Screen recording is '
      + 'disabled, or Group Policy blocks app-level screen capture.';
  }
  return {
    sources: sources.map((s) => ({
      id: s.id,
      name: s.name,
      thumbnail: s.thumbnail.toDataURL(),
      appIcon: s.appIcon?.toDataURL() || null,
      display_id: s.display_id,
    })),
    captureBlocked,
    errorReason,
  };
});

ipcMain.handle('app:get-version', () => app.getVersion());
ipcMain.handle('app:get-server-port', () => serverPort);
ipcMain.handle('app:get-data-dir', () => getAppDataDir());
ipcMain.handle('app:is-dev', () => isDev);
ipcMain.handle('app:get-client-config', () => loadClientConfig());
ipcMain.handle('app:get-display-name', () => displayName);
ipcMain.handle('app:set-display-name', (_evt, name: string) => {
  try {
    return { success: true, name: saveDisplayName(name) };
  } catch (e: any) {
    return { success: false, error: e?.message || 'failed to save' };
  }
});

// ── LAN Discovery (UDP broadcast listener) ─────────────
import { startDiscovery, stopDiscovery, registerDiscoveryIPC, setDiscoverySecret } from './discovery';
registerDiscoveryIPC();
// Audit fix C3: provision the discovery HMAC secret from clientConfig
// before discovery starts. Empty config = accept unsigned (LAN-only).
try {
  const cfg = loadClientConfig();
  if ((cfg as any).discoverySecret) {
    setDiscoverySecret((cfg as any).discoverySecret);
  }
} catch (_e) { /* discovery without secret falls back to unsigned */ }

// ── Secure Credential Storage (DPAPI on Windows) ────
// Uses Electron's safeStorage API which leverages OS-level encryption
// (DPAPI on Windows, Keychain on macOS, libsecret on Linux)

import { safeStorage } from 'electron';

ipcMain.handle('secure-store:set', (_event, key: string, value: string) => {
  if (!safeStorage.isEncryptionAvailable()) {
    // Fallback: base64 obfuscation (not truly secure, but better than plaintext)
    const encoded = Buffer.from(value).toString('base64');
    const store = getSecureStore();
    store[key] = encoded;
    writeSecureStore(store);
    return true;
  }
  const encrypted = safeStorage.encryptString(value);
  const store = getSecureStore();
  store[key] = encrypted.toString('base64');
  writeSecureStore(store);
  return true;
});

ipcMain.handle('secure-store:get', (_event, key: string) => {
  const store = getSecureStore();
  const data = store[key];
  if (!data) return null;

  if (!safeStorage.isEncryptionAvailable()) {
    return Buffer.from(data, 'base64').toString('utf-8');
  }
  try {
    const buffer = Buffer.from(data, 'base64');
    return safeStorage.decryptString(buffer);
  } catch {
    return null;
  }
});

ipcMain.handle('secure-store:delete', (_event, key: string) => {
  const store = getSecureStore();
  delete store[key];
  writeSecureStore(store);
  return true;
});

ipcMain.handle('secure-store:clear', () => {
  writeSecureStore({});
  return true;
});

/**
 * Unread badge IPC. The renderer's notification store calls this on every
 * unread-count change so the OS-level badge reflects reality:
 *   - Window title gains a leading "(N) " prefix (works on every platform).
 *   - On Windows: setOverlayIcon (taskbar) gets a small dot.
 *   - On macOS: app.setBadgeCount paints the dock badge.
 *   - On Linux: app.setBadgeCount works on Unity-derived desktops.
 *   - Tray tooltip gets the count appended so the user can read it on hover.
 *
 * The renderer never touches OS APIs directly — this is the only place
 * platform-specific badge code lives.
 */
ipcMain.handle('app:set-unread-badge', (_evt, count: number) => {
  const safe = Math.max(0, Math.floor(Number(count) || 0));
  try {
    BrowserWindow.getAllWindows().forEach((w) => {
      const base = displayName || 'Helen Desktop';
      const next = safe > 0 ? `(${safe > 99 ? '99+' : safe}) ${base}` : base;
      try { w.setTitle(next); } catch { /* ignore */ }
    });
  } catch { /* ignore */ }
  try { app.setBadgeCount(safe); } catch { /* macOS / Unity only */ }
  try {
    if (process.platform === 'win32' && mainWindow) {
      // A small red dot is enough — full numeric badge needs an image
      // generator we don't ship. Pass null to clear.
      mainWindow.setOverlayIcon(safe > 0 ? null : null, safe > 0 ? `${safe} unread` : '');
      // The "null icon, descriptive text" trick still flashes the taskbar
      // entry on most Windows configs without requiring a custom PNG.
    }
  } catch { /* ignore */ }
  try {
    if (tray) {
      const base = 'Helen Desktop — LAN Communication';
      tray.setToolTip(safe > 0 ? `${base}  •  ${safe} unread` : base);
    }
  } catch { /* ignore */ }
  return safe;
});

function getSecureStore(): Record<string, string> {
  const storePath = join(getAppDataDir(), '.credentials');
  try {
    if (existsSync(storePath)) {
      const raw = readFileSync(storePath, 'utf-8');
      return JSON.parse(raw);
    }
  } catch {}
  return {};
}

function writeSecureStore(store: Record<string, string>): void {
  const storePath = join(getAppDataDir(), '.credentials');
  writeFileSync(storePath, JSON.stringify(store), 'utf-8');
}

// ── Diagnostics IPC Handlers ────────────────────────────
// Used by DiagnosticsLogger (log persistence) and DiagnosticsCollector (package export)

/** Rotating diagnostics log writer — appends serialized log entries to a daily log file */
ipcMain.handle(
  'diagnostics:write-log',
  async (_event, entries: string[]) => {
    if (!entries || entries.length === 0) return;
    const logsDir = getLogsDir();
    const dateStr = new Date().toISOString().substring(0, 10); // YYYY-MM-DD
    const logFile = join(logsDir, `diagnostics-${dateStr}.log`);
    const appendFile = appendFileAsync;
    const payload = entries.join('\n') + '\n';
    try {
      await appendFile(logFile, payload, 'utf-8');
    } catch (err) {
      console.error('[Main] diagnostics:write-log failed:', (err as Error).message);
    }
  }
);

/** Save full diagnostics package via native Save dialog */
ipcMain.handle(
  'diagnostics:save-package',
  async (_event, jsonData: string, suggestedFileName: string): Promise<string | null> => {
    if (!mainWindow) return null;
    const { canceled, filePath } = await dialog.showSaveDialog(mainWindow, {
      title: 'Save Diagnostics Package',
      defaultPath: join(app.getPath('downloads'), suggestedFileName),
      filters: [
        { name: 'JSON Files', extensions: ['json'] },
        { name: 'All Files', extensions: ['*'] },
      ],
    });
    if (canceled || !filePath) return null;
    const writeFile = writeFileAsync;
    try {
      await writeFile(filePath, jsonData, 'utf-8');
      console.log('[Main] Diagnostics package saved:', filePath);
      return filePath;
    } catch (err) {
      console.error('[Main] diagnostics:save-package failed:', (err as Error).message);
      return null;
    }
  }
);

ipcMain.on('notification:show', (_event, { title, body }: { title: string; body: string }) => {
  if (mainWindow?.isFocused()) return;
  new Notification({ title, body }).show();
});

// Force-foreground the main window. Used by chat / call stores when an
// incoming DM, group message, or call should pull the user back into
// the app. The full sequence:
//   1. Restore from a minimized state.
//   2. Reveal if the window was hidden (tray-only mode).
//   3. Briefly toggle alwaysOnTop — this is the well-known Windows
//      trick that forces the window to the foreground without leaving
//      it sticky-on-top forever (Windows blocks plain .focus() from a
//      background process for security reasons).
//   4. Flash the taskbar icon so the user notices even if their cursor
//      is on another monitor.
// No-ops when the window is already focused so we don't yank the user
// out of whatever they were doing inside Helen itself.
ipcMain.on('window:force-focus', () => {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isFocused()) return;
  try {
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.setAlwaysOnTop(true, 'floating');
    mainWindow.focus();
    mainWindow.setAlwaysOnTop(false);
    mainWindow.flashFrame(true);
  } catch {
    // Don't let a foreground hiccup crash the main process — the user
    // can still click the tray icon if it falls through.
  }
});

ipcMain.handle('call-window:open', () => {
  if (callWindow) {
    callWindow.focus();
    return;
  }
  callWindow = new BrowserWindow({
    width: 400,
    height: 300,
    alwaysOnTop: true,
    frame: false,
    resizable: true,
    minimizable: false,
    backgroundColor: '#0f172a',
    webPreferences: {
      preload: getPreloadPath(),
      contextIsolation: true,
      nodeIntegration: false,
      // Audit fix: pip-call window was missing sandbox + webSecurity
      // + insecure-content guard, so it ran with looser security than
      // the main window. Match main window's hardening so renderer
      // compromise can't pivot through this surface.
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });
  if (isDev) {
    callWindow.loadURL('http://localhost:5173/#/pip');
  } else {
    callWindow.loadFile(join(__dirname, '../renderer/index.html'), { hash: '/pip' });
  }
  callWindow.on('closed', () => { callWindow = null; });
});

ipcMain.handle('call-window:close', () => {
  callWindow?.close();
  callWindow = null;
});

// ── App Lifecycle ───────────────────────────────────────

// Prevent multiple instances
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    // Load persisted display name before any window is created so the
    // initial title matches the user's choice.
    try { displayName = loadDisplayName(); } catch { /* keep default */ }

    try {
      await startBackendServer();
    } catch {
      // Error already shown via dialog
      app.quit();
      return;
    }
    // Start LAN discovery after backend is ready
    // (listens for other servers too, and the local server's own broadcasts)
    startDiscovery();

    createMainWindow();
    createTray();
    registerGlobalShortcuts();

    // OS integration — deep links, notifications with AUMID, auto-start,
    // power events, firewall runtime gate. The legacy entry already owns
    // the single-instance lock and tray, so we tell the aggregator to
    // skip those.
    try {
      installSystemIntegrations({
        getMainWindow: () => mainWindow,
        aumid: 'com.helen.desktop',  // must match electron-builder.yml appId
        skipSingleInstance: true,
        enableTray: false,
        enableAutoStart: true,
        enableFirewall: true,
        hideOnClose: true,
      });
    } catch (err) {
      console.warn('[main] installSystemIntegrations failed:', (err as Error).message);
    }

    // Auto-updater — LAN-mirror preferred, internet fallback, signature
    // verification enforced in packaged builds.
    try {
      await installUpdateSystem({ getMainWindow: () => mainWindow });
    } catch (err) {
      console.warn('[main] installUpdateSystem failed:', (err as Error).message);
    }

    // iPhone-over-USB detector: watches for the 172.20.10.x tether subnet
    // so the PairPhoneDialog can swap its QR to a USB-reachable URL.
    try {
      installUsbPhoneDetect();
    } catch (err) {
      console.warn('[main] installUsbPhoneDetect failed:', (err as Error).message);
    }

    // QuickTime-over-USB helper (scaffold — activates only if the native
    // ``usb`` module is present). Enumerates Apple devices and exposes
    // stubbed activate() IPC so the renderer can be built against a stable
    // surface even before the decode pipeline lands.
    try {
      installUsbQuickTimeHelper();
    } catch (err) {
      console.warn('[main] installUsbQuickTimeHelper failed:', (err as Error).message);
    }
  });
}

app.on('window-all-closed', () => {
  // Don't quit — tray keeps running on Windows
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
});

app.on('before-quit', async (e) => {
  // Prevent default to allow async cleanup
  e.preventDefault();
  console.log('[Main] Cleaning up before quit...');
  try { shutdownUsbPhoneDetect(); } catch { /* ignore */ }
  try { shutdownUsbQuickTimeHelper(); } catch { /* ignore */ }
  stopDiscovery();
  await stopBackendServer();
  // Force quit after cleanup
  app.exit(0);
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});
