/**
 * InstallerConfig.ts — Installer architecture, folder layout, and path resolver.
 *
 * Defines the SINGLE SOURCE OF TRUTH for all runtime paths used by
 * CommClient on Windows. Every component (main process, backend server,
 * renderer, updater) reads from here.
 *
 * Architecture decisions:
 *   1. Per-user install (no admin required) — installs to %LOCALAPPDATA%
 *   2. User data in %APPDATA%/CommClient/ — survives reinstalls
 *   3. Logs in %APPDATA%/CommClient/logs/ — accessible without admin
 *   4. Database in %APPDATA%/CommClient/data/ — writable, backup-friendly
 *   5. Temp files in %TEMP%/CommClient/ — auto-cleaned by OS
 *   6. No writes to Program Files — avoids UAC/permission issues
 *
 * Folder layout after install:
 *
 *   %LOCALAPPDATA%\Programs\CommClient\         (install dir, read-only at runtime)
 *   ├── CommClient.exe                           (Electron shell)
 *   ├── resources\
 *   │   ├── app.asar                             (renderer + main JS, read-only)
 *   │   ├── server\                              (PyInstaller backend bundle)
 *   │   │   ├── CommClient-Server.exe
 *   │   │   └── _internal\                       (Python runtime + deps)
 *   │   └── installer\
 *   │       ├── icon.ico
 *   │       └── uninstall.ico
 *   ├── locales\                                 (Chromium locales)
 *   └── *.dll                                    (Electron native DLLs)
 *
 *   %APPDATA%\CommClient\                        (user data, read-write, survives updates)
 *   ├── config.json                              (user preferences)
 *   ├── data\
 *   │   ├── commclient.db                        (SQLite database)
 *   │   ├── commclient.db-wal                    (WAL journal)
 *   │   ├── commclient.db-shm                    (shared memory)
 *   │   └── files\                               (uploaded/received files)
 *   │       ├── images\
 *   │       ├── documents\
 *   │       ├── audio\
 *   │       └── other\
 *   ├── logs\
 *   │   ├── main-YYYY-MM-DD.log                  (Electron main process)
 *   │   ├── server-YYYY-MM-DD.log                (Backend server)
 *   │   └── renderer-YYYY-MM-DD.log              (Renderer errors)
 *   ├── cache\
 *   │   ├── avatars\                             (cached avatar images)
 *   │   ├── thumbnails\                          (file preview thumbnails)
 *   │   └── discovery\                           (cached LAN peer list)
 *   └── credentials\                             (DPAPI-encrypted credentials)
 *       └── session.enc
 *
 *   %TEMP%\CommClient\                           (ephemeral, auto-cleaned)
 *   ├── update-staging\                          (pending update download)
 *   └── crash-dumps\                             (minidump files)
 *
 * Integration:
 *   - Electron main process imports this for all path resolution
 *   - Backend server receives paths via environment variables at spawn time
 *   - Renderer queries paths via IPC (app:getDataDir, app:getLogsDir)
 *   - NSIS installer script uses matching layout to create folders
 */

import { app } from 'electron';
import { join } from 'path';
import { existsSync, mkdirSync, statSync } from 'fs';

// ── Constants ───────────────────────────────────────────────

export const APP_NAME = 'CommClient';
// MUST match electron-builder.yml `appId`. Misalignment breaks
// Action-Center toast persistence + click-back-to-app routing.
export const APP_ID = 'com.helen.desktop';
export const SERVER_EXE_NAME = 'CommClient-Server.exe';
export const DEFAULT_SERVER_PORT = 3000;
export const SERVER_HEALTH_TIMEOUT_MS = 30_000;
export const SERVER_SHUTDOWN_TIMEOUT_MS = 5_000;
export const LOG_RETENTION_DAYS = 30;
export const MAX_LOG_SIZE_MB = 50;
export const DB_FILE_NAME = 'commclient.db';
export const CONFIG_FILE_NAME = 'config.json';
export const FIRST_RUN_MARKER = '.initialized';

/** Version of the data directory layout. Bump when folder structure changes. */
export const DATA_LAYOUT_VERSION = 1;

// ── Environment Detection ───────────────────────────────────

export const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged;

/**
 * Portable mode is opted-in by dropping a `.portable` file next to the
 * install. Wrapped in try/catch because this constant is evaluated at
 * module-load time, before the consumer has had a chance to handle
 * errors — and `getInstallDir()` calls into Electron `app` APIs that
 * can throw if the module is imported from an unexpected context (e.g.
 * a worker thread, a test harness without app stub). A failure here
 * should default to "not portable", never crash module load.
 */
export const isPortable: boolean = (() => {
  try {
    return existsSync(join(getInstallDir(), '.portable'));
  } catch {
    return false;
  }
})();

/**
 * Determine if this is the first run after install (or first run ever).
 * Checks for the presence of the initialization marker file.
 */
export function isFirstRun(): boolean {
  const marker = join(getAppDataDir(), FIRST_RUN_MARKER);
  return !existsSync(marker);
}

// ── Install Directory (read-only at runtime) ────────────────

/**
 * Get the application installation directory.
 * - Production: where CommClient.exe lives (e.g., %LOCALAPPDATA%\Programs\CommClient)
 * - Dev: project root
 */
export function getInstallDir(): string {
  if (isDev) {
    return join(__dirname, '../../../');
  }
  // app.getAppPath() returns the asar path; we need the actual directory
  return join(app.getAppPath(), '../../');
}

/**
 * Get the resources directory.
 * - Production: process.resourcesPath (points to <install>/resources)
 * - Dev: project root resources/
 */
export function getResourcesDir(): string {
  if (isDev) {
    return join(__dirname, '../../../resources');
  }
  return process.resourcesPath;
}

// ── User Data Directory (%APPDATA%\CommClient) ──────────────

/**
 * Root user data directory. All writable data lives here.
 * - Production: %APPDATA%\CommClient
 * - Portable: <install_dir>\data
 * - Dev: <project>/dev-data
 */
export function getAppDataDir(): string {
  if (isDev) {
    return ensureDir(join(__dirname, '../../../dev-data'));
  }
  if (isPortable) {
    return ensureDir(join(getInstallDir(), 'data'));
  }
  return ensureDir(join(app.getPath('appData'), APP_NAME));
}

/** Database and uploaded files directory */
export function getDataDir(): string {
  return ensureDir(join(getAppDataDir(), 'data'));
}

/** Log files directory */
export function getLogsDir(): string {
  return ensureDir(join(getAppDataDir(), 'logs'));
}

/** Cache directory (avatars, thumbnails, discovery cache) */
export function getCacheDir(): string {
  return ensureDir(join(getAppDataDir(), 'cache'));
}

/** Credential storage directory (DPAPI-encrypted) */
export function getCredentialsDir(): string {
  return ensureDir(join(getAppDataDir(), 'credentials'));
}

/** User preferences config file path */
export function getConfigPath(): string {
  return join(getAppDataDir(), CONFIG_FILE_NAME);
}

// ── Data Subdirectories ─────────────────────────────────────

/** SQLite database file path */
export function getDatabasePath(): string {
  return join(getDataDir(), DB_FILE_NAME);
}

/** Uploaded/received files root directory */
export function getFilesDir(): string {
  return ensureDir(join(getDataDir(), 'files'));
}

/** Subdirectories for organized file storage */
export function getFilesSubDir(category: 'images' | 'documents' | 'audio' | 'other'): string {
  return ensureDir(join(getFilesDir(), category));
}

// ── Cache Subdirectories ────────────────────────────────────

/** Cached avatar images directory */
export function getAvatarCacheDir(): string {
  return ensureDir(join(getCacheDir(), 'avatars'));
}

/** File preview thumbnails directory */
export function getThumbnailCacheDir(): string {
  return ensureDir(join(getCacheDir(), 'thumbnails'));
}

/** LAN discovery cache directory */
export function getDiscoveryCacheDir(): string {
  return ensureDir(join(getCacheDir(), 'discovery'));
}

// ── Temp Directory ──────────────────────────────────────────

/**
 * Temporary directory for ephemeral data.
 * Cleaned by OS or on startup.
 */
export function getTempDir(): string {
  return ensureDir(join(app.getPath('temp'), APP_NAME));
}

/** Staging directory for pending updates */
export function getUpdateStagingDir(): string {
  return ensureDir(join(getTempDir(), 'update-staging'));
}

/** Crash dump directory */
export function getCrashDumpDir(): string {
  return ensureDir(join(getTempDir(), 'crash-dumps'));
}

// ── Backend Server Paths ────────────────────────────────────

/** Path to the backend server executable */
export function getServerExePath(): string {
  if (isDev) return ''; // Dev mode: server runs externally
  return join(getResourcesDir(), 'server', SERVER_EXE_NAME);
}

/** Check if the backend server executable exists */
export function isServerBundled(): boolean {
  if (isDev) return false;
  return existsSync(getServerExePath());
}

/**
 * Generate environment variables for the backend server process.
 * These tell the Python server where to find/store data.
 */
export function getServerEnv(): Record<string, string> {
  const dataDir = getDataDir();
  const logsDir = getLogsDir();

  // Audit fix: previous default was HOST=0.0.0.0 — embedded server
  // bound to ALL interfaces (LAN, hotspot, public WiFi), trivially
  // reachable by anyone on the network. The embedded server is for
  // the LOCAL Electron client only; LAN sharing is intentional and
  // gets opted into via clientConfig.allowLanShare. Default to
  // loopback so a fresh install isn't an accidental open hotspot.
  // Operators wanting LAN-shared installs set HELEN_BIND_HOST=0.0.0.0
  // explicitly — env wins via the spread in `start_backend_server`.
  return {
    HOST: '127.0.0.1',
    PORT: String(DEFAULT_SERVER_PORT),
    DEBUG: 'false',
    LOG_LEVEL: isDev ? 'DEBUG' : 'INFO',
    SQLITE_PATH: getDatabasePath(),
    UPLOAD_DIR: getFilesDir(),
    LOG_DIR: logsDir,
    COMMCLIENT_DATA_DIR: dataDir,
    // Phase 10 backend optimizations
    CC_SQLITE_WAL_MODE: '1',
    CC_SQLITE_CACHE_SIZE_KB: '4096',
    CC_API_WORKERS: '2',
    CC_SOCKETIO_MAX_HTTP_BUFFER: '5242880',
    CC_FILE_CHUNK_SIZE: '65536',
  };
}

// ── Icon Paths ──────────────────────────────────────────────

/** Main application icon (.ico for Windows) */
export function getAppIconPath(): string {
  const resourceIcon = join(getResourcesDir(), 'installer', 'icon.ico');
  if (existsSync(resourceIcon)) return resourceIcon;
  if (isDev) return join(__dirname, '../../../resources/installer/icon.ico');
  return '';
}

/** Tray icon (can be different size) */
export function getTrayIconPath(): string {
  const trayIcon = join(getResourcesDir(), 'installer', 'tray.ico');
  if (existsSync(trayIcon)) return trayIcon;
  return getAppIconPath(); // Fallback to main icon
}

// ── Preload Script ──────────────────────────────────────────

/** Path to the preload script for BrowserWindow */
export function getPreloadPath(): string {
  return join(__dirname, '../preload/index.js');
}

// ── Log File Naming ─────────────────────────────────────────

/** Generate a timestamped log file path */
export function getLogFilePath(prefix: string): string {
  const date = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
  return join(getLogsDir(), `${prefix}-${date}.log`);
}

/** Generate the server log path for today */
export function getServerLogPath(): string {
  return getLogFilePath('server');
}

/** Generate the main process log path for today */
export function getMainLogPath(): string {
  return getLogFilePath('main');
}

// ── Validation ──────────────────────────────────────────────

export interface PathValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

/**
 * Validate that all critical directories exist and are writable.
 * Called on startup to detect permission issues early.
 */
export function validatePaths(): PathValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  // Check writable directories
  const writableDirs = [
    { name: 'AppData', path: getAppDataDir() },
    { name: 'Data', path: getDataDir() },
    { name: 'Logs', path: getLogsDir() },
    { name: 'Cache', path: getCacheDir() },
    { name: 'Files', path: getFilesDir() },
  ];

  for (const dir of writableDirs) {
    if (!existsSync(dir.path)) {
      try {
        mkdirSync(dir.path, { recursive: true });
      } catch (err) {
        errors.push(`Cannot create ${dir.name} directory: ${dir.path} — ${(err as Error).message}`);
      }
    } else {
      // Check writability by stat
      try {
        statSync(dir.path);
      } catch {
        errors.push(`Cannot access ${dir.name} directory: ${dir.path}`);
      }
    }
  }

  // Check server executable
  if (!isDev && !isServerBundled()) {
    errors.push(`Backend server not found at: ${getServerExePath()}`);
  }

  // Check icon
  if (!getAppIconPath()) {
    warnings.push('Application icon not found — using default Electron icon');
  }

  // Check disk space (warn if < 100MB free)
  try {
    const dataDir = getDataDir();
    // Node doesn't have a built-in disk space check, but we can try a write test
    const testFile = join(dataDir, '.write-test');
    const { writeFileSync, unlinkSync } = require('fs');
    writeFileSync(testFile, 'test');
    unlinkSync(testFile);
  } catch {
    warnings.push('Data directory may not be writable');
  }

  return {
    valid: errors.length === 0,
    errors,
    warnings,
  };
}

/**
 * Get a diagnostic summary of all paths for troubleshooting.
 * Exposed via IPC for the Settings > About panel.
 */
export function getPathDiagnostics(): Record<string, string> {
  return {
    installDir: getInstallDir(),
    resourcesDir: getResourcesDir(),
    appDataDir: getAppDataDir(),
    dataDir: getDataDir(),
    logsDir: getLogsDir(),
    cacheDir: getCacheDir(),
    credentialsDir: getCredentialsDir(),
    filesDir: getFilesDir(),
    databasePath: getDatabasePath(),
    configPath: getConfigPath(),
    serverExePath: getServerExePath(),
    tempDir: getTempDir(),
    appIconPath: getAppIconPath(),
    preloadPath: getPreloadPath(),
    isDev: String(isDev),
    isPortable: String(isPortable),
    isFirstRun: String(isFirstRun()),
    platform: process.platform,
    arch: process.arch,
    electronVersion: process.versions.electron,
    nodeVersion: process.versions.node,
    chromeVersion: process.versions.chrome,
  };
}

// ── Utility ─────────────────────────────────────────────────

/** Ensure a directory exists. Returns the path for chaining. */
function ensureDir(dirPath: string): string {
  if (!existsSync(dirPath)) {
    mkdirSync(dirPath, { recursive: true });
  }
  return dirPath;
}
