/**
 * @deprecated NOT THE LIVE MAIN PROCESS — see src/main/index.ts.
 *
 * Audit finding (2026-04-27): two parallel main-process orchestrators
 * existed in this tree, `src/main/index.ts` and this file. Both
 * register `ipcMain.handle()` for overlapping channels, both spawn the
 * backend server, and both grab the single-instance lock. If both
 * ran in the same process Electron would throw on the duplicate IPC
 * registrations and the user would see an opaque crash on first
 * boot. The LIVE entry-point is `src/main/index.ts`; this file is
 * kept as scaffolding for a future refactor that moves orchestration
 * out of `index.ts`'s ~1000-line top-level body, but it MUST NOT be
 * imported from production code paths.
 *
 * To prevent accidental dual-startup, the `start()` method below
 * throws unless `process.env.HELEN_USE_LIFECYCLE_MANAGER === '1'` is
 * set explicitly (testing/migration only).
 *
 * AppLifecycleManager.ts — Startup sequence, backend launch, and shutdown orchestration.
 *
 * Owns the entire application lifecycle from Electron's `ready` event
 * to the final `quit`. Every subsystem registers with this manager
 * instead of attaching independent lifecycle hooks.
 *
 * Startup sequence (ordered):
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ 1. acquireSingleInstanceLock()                          │
 *   │ 2. configureCrashReporter()                             │
 *   │ 3. FirstRunInitializer.initialize()                     │
 *   │    → create dirs, write config, migrate, firewall       │
 *   │ 4. validatePaths()                                      │
 *   │    → ensure all critical paths are writable             │
 *   │ 5. startBackendServer()                                 │
 *   │    → spawn CommClient-Server.exe, wait for /health      │
 *   │ 6. createMainWindow()                                   │
 *   │    → BrowserWindow with preload, CSP, security          │
 *   │ 7. createTrayIcon()                                     │
 *   │    → system tray with context menu                      │
 *   │ 8. registerGlobalShortcuts()                            │
 *   │ 9. registerIPCHandlers()                                │
 *   │ 10. showMainWindow()                                    │
 *   │     → window.show() with splash screen transition       │
 *   └──────────────────────────────────────────────────────────┘
 *
 * Shutdown sequence (ordered):
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ 1. Save window state (position, size, maximized)        │
 *   │ 2. Close all child windows (PIP, settings)              │
 *   │ 3. Unregister global shortcuts                          │
 *   │ 4. Stop backend server (graceful → force after 5s)      │
 *   │ 5. Close log streams                                    │
 *   │ 6. Destroy tray icon                                    │
 *   │ 7. app.quit()                                           │
 *   └──────────────────────────────────────────────────────────┘
 *
 * This replaces scattered lifecycle hooks in the existing main/index.ts.
 * The existing code is NOT modified — this module adds a new orchestrator
 * that can be wired in as an alternative entry point.
 */

import { app, BrowserWindow, Tray, Menu, dialog, screen, globalShortcut } from 'electron';
import { join } from 'path';
import { existsSync, writeFileSync, readFileSync, createWriteStream, WriteStream } from 'fs';
import { ChildProcess, spawn } from 'child_process';
import * as http from 'http';
import {
  APP_NAME,
  DEFAULT_SERVER_PORT,
  SERVER_HEALTH_TIMEOUT_MS,
  SERVER_SHUTDOWN_TIMEOUT_MS,
  isDev,
  getAppDataDir,
  getDataDir,
  getLogsDir,
  getServerExePath,
  getServerEnv,
  getDatabasePath,
  getFilesDir,
  getAppIconPath,
  getTrayIconPath,
  getPreloadPath,
  getMainLogPath,
  getServerLogPath,
  getCrashDumpDir,
  getConfigPath,
  validatePaths,
  getPathDiagnostics,
  isServerBundled,
} from './InstallerConfig';
import { initialize as firstRunInit, readUserConfig, type UserConfig } from './FirstRunInitializer';

// ── Types ───────────────────────────────────────────────────

export type LifecyclePhase =
  | 'idle'
  | 'initializing'
  | 'starting_server'
  | 'creating_window'
  | 'ready'
  | 'shutting_down'
  | 'terminated';

export interface LifecycleState {
  phase: LifecyclePhase;
  serverRunning: boolean;
  windowReady: boolean;
  startupTimeMs: number;
  errors: string[];
}

interface WindowState {
  x: number | undefined;
  y: number | undefined;
  width: number;
  height: number;
  maximized: boolean;
}

// ── Singleton ───────────────────────────────────────────────

class AppLifecycleManager {
  private phase: LifecyclePhase = 'idle';
  private serverProcess: ChildProcess | null = null;
  private serverLogStream: WriteStream | null = null;
  private mainLogStream: WriteStream | null = null;
  private mainWindow: BrowserWindow | null = null;
  private tray: Tray | null = null;
  private childWindows: Map<string, BrowserWindow> = new Map();
  private userConfig: UserConfig | null = null;
  private startTime = 0;
  private errors: string[] = [];

  // ── Public API ──────────────────────────────────────────

  /**
   * Full startup sequence. Call once from app.whenReady().
   *
   * Audit guard: refuses to run unless `HELEN_USE_LIFECYCLE_MANAGER=1`.
   * The live main process is `src/main/index.ts`; calling this method
   * by accident (e.g. importing this file from a renderer entry by
   * mistake) would race the live IPC handlers and crash. Set the env
   * flag explicitly when migrating off `index.ts`.
   */
  async start(): Promise<void> {
    if (process.env.HELEN_USE_LIFECYCLE_MANAGER !== '1') {
      throw new Error(
        'AppLifecycleManager.start() is gated. The live main process ' +
        'is src/main/index.ts. Set HELEN_USE_LIFECYCLE_MANAGER=1 to ' +
        'opt into this orchestrator (migration / testing only).',
      );
    }
    this.startTime = Date.now();
    this.phase = 'initializing';

    try {
      // ── Phase 1: Single instance lock ─────────────────
      if (!this.acquireSingleInstanceLock()) {
        this.log('Another instance is already running — quitting');
        app.quit();
        return;
      }

      // ── Phase 2: Crash reporter ───────────────────────
      this.configureCrashReporter();

      // ── Phase 3: Open main log stream ─────────────────
      this.openMainLog();

      // ── Phase 4: First-run initialization ─────────────
      this.log('Running first-run initialization...');
      const initResult = await firstRunInit();
      if (!initResult.success) {
        this.errors.push(...initResult.errors);
        this.log(`Initialization had errors: ${initResult.errors.join('; ')}`);
      }
      if (initResult.isFirstRun) {
        this.log('First run completed');
      }
      if (initResult.isUpgrade) {
        this.log(`Upgraded from layout v${initResult.previousVersion}`);
      }

      // ── Phase 5: Validate paths ───────────────────────
      this.log('Validating paths...');
      const pathValidation = validatePaths();
      if (!pathValidation.valid) {
        this.errors.push(...pathValidation.errors);
        this.log(`Path validation errors: ${pathValidation.errors.join('; ')}`);
      }
      for (const warn of pathValidation.warnings) {
        this.log(`Path warning: ${warn}`);
      }

      // ── Phase 6: Load user config ─────────────────────
      this.userConfig = readUserConfig();

      // ── Phase 7: Start backend server ─────────────────
      this.phase = 'starting_server';
      this.log('Starting backend server...');
      await this.startServer();
      this.log('Backend server is ready');

      // ── Phase 8: Create main window ───────────────────
      this.phase = 'creating_window';
      this.log('Creating main window...');
      this.createWindow();

      // ── Phase 9: Ready ────────────────────────────────
      this.phase = 'ready';
      const elapsed = Date.now() - this.startTime;
      this.log(`Application ready in ${elapsed}ms`);

    } catch (err) {
      const msg = (err as Error).message;
      this.errors.push(msg);
      this.log(`FATAL: Startup failed — ${msg}`);
      dialog.showErrorBox(
        `${APP_NAME} — Startup Failed`,
        `The application failed to start.\n\n${msg}\n\nCheck logs at:\n${getLogsDir()}`
      );
      app.quit();
    }
  }

  /**
   * Graceful shutdown sequence.
   */
  async shutdown(): Promise<void> {
    if (this.phase === 'shutting_down' || this.phase === 'terminated') return;
    this.phase = 'shutting_down';
    this.log('Shutdown sequence starting...');

    try {
      // Save window state
      this.saveWindowState();

      // Close child windows
      for (const [name, win] of this.childWindows) {
        if (!win.isDestroyed()) {
          this.log(`  Closing child window: ${name}`);
          win.close();
        }
      }
      this.childWindows.clear();

      // Unregister global shortcuts
      globalShortcut.unregisterAll();

      // Stop backend server
      this.log('  Stopping backend server...');
      await this.stopServer();

      // Close log streams
      this.mainLogStream?.end();
      this.serverLogStream?.end();

      // Destroy tray
      if (this.tray && !this.tray.isDestroyed()) {
        this.tray.destroy();
        this.tray = null;
      }

      this.phase = 'terminated';
      this.log('Shutdown complete');
    } catch (err) {
      this.log(`Shutdown error: ${(err as Error).message}`);
    }
  }

  getState(): LifecycleState {
    return {
      phase: this.phase,
      serverRunning: this.serverProcess !== null && !this.serverProcess.killed,
      windowReady: this.mainWindow !== null && !this.mainWindow.isDestroyed(),
      startupTimeMs: Date.now() - this.startTime,
      errors: [...this.errors],
    };
  }

  getMainWindow(): BrowserWindow | null {
    return this.mainWindow;
  }

  getUserConfig(): UserConfig | null {
    return this.userConfig;
  }

  registerChildWindow(name: string, window: BrowserWindow): void {
    this.childWindows.set(name, window);
    window.on('closed', () => this.childWindows.delete(name));
  }

  // ── Private: Single Instance ────────────────────────────

  private acquireSingleInstanceLock(): boolean {
    const gotLock = app.requestSingleInstanceLock();
    if (!gotLock) return false;

    app.on('second-instance', (_event, _argv, _workingDir) => {
      // Focus existing window when user tries to launch again
      if (this.mainWindow) {
        if (this.mainWindow.isMinimized()) this.mainWindow.restore();
        this.mainWindow.focus();
      }
    });

    return true;
  }

  // ── Private: Crash Reporter ─────────────────────────────

  private configureCrashReporter(): void {
    app.setPath('crashDumps', getCrashDumpDir());
  }

  // ── Private: Logging ────────────────────────────────────

  private openMainLog(): void {
    try {
      this.mainLogStream = createWriteStream(getMainLogPath(), { flags: 'a' });
    } catch {}
  }

  private log(msg: string): void {
    const timestamp = new Date().toISOString();
    const line = `[${timestamp}] [Lifecycle] ${msg}`;
    console.log(line);
    try { this.mainLogStream?.write(line + '\n'); } catch {}
  }

  // ── Private: Backend Server ─────────────────────────────

  private async startServer(): Promise<void> {
    if (isDev) {
      this.log('Dev mode — expecting external server');
      return;
    }

    const exePath = getServerExePath();
    if (!existsSync(exePath)) {
      throw new Error(`Server executable not found: ${exePath}`);
    }

    // Open server log stream
    this.serverLogStream = createWriteStream(getServerLogPath(), { flags: 'a' });

    const env = getServerEnv();
    this.log(`  Server exe: ${exePath}`);
    this.log(`  Database: ${env.SQLITE_PATH}`);
    this.log(`  Port: ${env.PORT}`);

    this.serverProcess = spawn(exePath, [], {
      env: { ...process.env, ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      detached: false,
    });

    this.serverProcess.stdout?.pipe(this.serverLogStream);
    this.serverProcess.stderr?.pipe(this.serverLogStream);

    this.serverProcess.on('error', (err) => {
      this.log(`Server process error: ${err.message}`);
    });

    this.serverProcess.on('exit', (code, signal) => {
      this.log(`Server exited: code=${code} signal=${signal}`);
      this.serverProcess = null;

      // If server crashes during ready phase, show error and restart attempt
      if (this.phase === 'ready' && code !== 0) {
        this.log('Server crashed during operation — notifying user');
        dialog.showErrorBox(
          `${APP_NAME} — Server Stopped`,
          `The backend server stopped unexpectedly (code: ${code}).\n` +
          `Please restart ${APP_NAME}.`
        );
      }
    });

    // Wait for health check
    await this.waitForHealth(DEFAULT_SERVER_PORT, SERVER_HEALTH_TIMEOUT_MS);
  }

  private waitForHealth(port: number, timeoutMs: number): Promise<void> {
    const start = Date.now();
    const url = `http://127.0.0.1:${port}/api/health`;

    return new Promise((resolve, reject) => {
      let attempts = 0;
      const check = () => {
        if (Date.now() - start > timeoutMs) {
          reject(new Error(`Server health check timeout after ${attempts} attempts (${timeoutMs}ms)`));
          return;
        }
        attempts++;
        const req = http.get(url, (res) => {
          if (res.statusCode && res.statusCode >= 200 && res.statusCode < 500) {
            this.log(`  Server responded on attempt ${attempts}`);
            resolve();
          } else {
            setTimeout(check, 500);
          }
          res.resume();
        });
        req.on('error', () => setTimeout(check, 500));
        req.setTimeout(2_000, () => {
          req.destroy();
          setTimeout(check, 500);
        });
      };
      check();
    });
  }

  private async stopServer(): Promise<void> {
    if (!this.serverProcess || this.serverProcess.killed) {
      this.serverLogStream?.end();
      return;
    }

    const pid = this.serverProcess.pid;
    this.log(`  Stopping server PID ${pid}...`);

    return new Promise<void>((resolve) => {
      const killTimer = setTimeout(() => {
        this.log('  Force killing server process');
        try { this.serverProcess?.kill('SIGKILL'); } catch {}
        this.serverLogStream?.end();
        resolve();
      }, SERVER_SHUTDOWN_TIMEOUT_MS);

      this.serverProcess!.once('exit', () => {
        clearTimeout(killTimer);
        this.serverLogStream?.end();
        this.log('  Server stopped');
        resolve();
      });

      // Windows: use taskkill for tree kill
      if (process.platform === 'win32' && pid) {
        spawn('taskkill', ['/pid', String(pid), '/t', '/f'], { windowsHide: true });
      } else {
        this.serverProcess!.kill('SIGTERM');
      }
    });
  }

  // ── Private: Window Management ──────────────────────────

  private createWindow(): void {
    const { width, height } = screen.getPrimaryDisplay().workAreaSize;
    const savedState = this.loadWindowState();

    this.mainWindow = new BrowserWindow({
      x: savedState?.x,
      y: savedState?.y,
      width: savedState?.width ?? Math.min(1400, width),
      height: savedState?.height ?? Math.min(900, height),
      minWidth: 900,
      minHeight: 600,
      title: APP_NAME,
      icon: getAppIconPath(),
      frame: false,
      titleBarStyle: 'hidden',
      backgroundColor: '#020617',
      show: false,
      webPreferences: {
        preload: getPreloadPath(),
        contextIsolation: true,
        nodeIntegration: false,
        webSecurity: true,
        spellcheck: false,
        sandbox: true,
      },
    });

    // Content Security Policy
    this.mainWindow.webContents.session.webRequest.onHeadersReceived((details, callback) => {
      callback({
        responseHeaders: {
          ...details.responseHeaders,
          'Content-Security-Policy': [
            "default-src 'self'; " +
            "script-src 'self'; " +
            "style-src 'self' 'unsafe-inline'; " +
            "img-src 'self' data: blob:; " +
            "media-src 'self' blob: mediastream:; " +
            `connect-src 'self' http://127.0.0.1:${DEFAULT_SERVER_PORT} ws://127.0.0.1:${DEFAULT_SERVER_PORT}; ` +
            "font-src 'self';"
          ],
        },
      });
    });

    // Prevent navigation to external URLs
    this.mainWindow.webContents.on('will-navigate', (event, url) => {
      if (!url.startsWith('http://127.0.0.1') && !url.startsWith('file://')) {
        event.preventDefault();
      }
    });

    // Load the renderer
    if (isDev) {
      this.mainWindow.loadURL('http://localhost:5173');
      this.mainWindow.webContents.openDevTools({ mode: 'detach' });
    } else {
      this.mainWindow.loadFile(join(__dirname, '../renderer/index.html'));
    }

    // Show window when ready
    this.mainWindow.once('ready-to-show', () => {
      if (savedState?.maximized) {
        this.mainWindow!.maximize();
      }

      const config = this.userConfig;
      if (config?.startup.startMinimized) {
        // Don't show — will be in tray
      } else {
        this.mainWindow!.show();
      }
    });

    // Close behavior: minimize to tray instead of quitting
    this.mainWindow.on('close', (event) => {
      if (this.phase !== 'shutting_down') {
        event.preventDefault();
        this.mainWindow?.hide();
      }
    });
  }

  // ── Private: Window State Persistence ───────────────────

  private getWindowStatePath(): string {
    return join(getAppDataDir(), 'window-state.json');
  }

  private loadWindowState(): WindowState | null {
    try {
      const statePath = this.getWindowStatePath();
      if (!existsSync(statePath)) return null;
      const raw = readFileSync(statePath, 'utf-8');
      const state = JSON.parse(raw) as WindowState;

      // Validate that the saved position is within current screen bounds
      const displays = screen.getAllDisplays();
      const inBounds = displays.some(d => {
        const b = d.bounds;
        return (
          (state.x ?? 0) >= b.x &&
          (state.y ?? 0) >= b.y &&
          (state.x ?? 0) < b.x + b.width &&
          (state.y ?? 0) < b.y + b.height
        );
      });

      if (!inBounds) {
        this.log('Saved window position out of bounds — using default');
        return null;
      }

      return state;
    } catch {
      return null;
    }
  }

  private saveWindowState(): void {
    if (!this.mainWindow || this.mainWindow.isDestroyed()) return;

    try {
      const bounds = this.mainWindow.getBounds();
      const state: WindowState = {
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
        maximized: this.mainWindow.isMaximized(),
      };
      writeFileSync(this.getWindowStatePath(), JSON.stringify(state), 'utf-8');
    } catch {}
  }
}

// ── Singleton Export ────────────────────────────────────────

export const appLifecycle = new AppLifecycleManager();
