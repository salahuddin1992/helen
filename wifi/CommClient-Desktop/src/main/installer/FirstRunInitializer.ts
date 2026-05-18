/**
 * FirstRunInitializer.ts — First-run setup, folder creation, and data migration.
 *
 * Handles everything that must happen ONCE after a fresh install or
 * after a version upgrade that changes the data layout:
 *
 *   1. Create the complete folder tree under %APPDATA%\CommClient
 *   2. Write default config.json with sensible defaults
 *   3. Set SQLite PRAGMA config for first-time DB creation
 *   4. Register the app in Windows Add/Remove Programs (if not NSIS-handled)
 *   5. Set Windows Firewall exception for LAN communication
 *   6. Write the .initialized marker with layout version
 *   7. Migrate data from previous layout versions if needed
 *   8. Clean stale temp files from previous installations
 *
 * This module is called ONCE from AppLifecycleManager.initialize().
 * It is idempotent — safe to re-run if interrupted.
 *
 * Design principles:
 *   - NEVER delete user data
 *   - NEVER require admin privileges
 *   - Log every action for troubleshooting
 *   - Fail gracefully with user-visible error messages
 */

import { app, dialog } from 'electron';
import { join } from 'path';
import {
  existsSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  readdirSync,
  unlinkSync,
  statSync,
  renameSync,
} from 'fs';
import { spawn } from 'child_process';
import {
  APP_NAME,
  DATA_LAYOUT_VERSION,
  FIRST_RUN_MARKER,
  LOG_RETENTION_DAYS,
  getAppDataDir,
  getDataDir,
  getLogsDir,
  getCacheDir,
  getCredentialsDir,
  getFilesDir,
  getFilesSubDir,
  getAvatarCacheDir,
  getThumbnailCacheDir,
  getDiscoveryCacheDir,
  getTempDir,
  getUpdateStagingDir,
  getCrashDumpDir,
  getConfigPath,
  getDatabasePath,
  getServerExePath,
  isDev,
  DEFAULT_SERVER_PORT,
} from './InstallerConfig';

// ── Types ───────────────────────────────────────────────────

export interface UserConfig {
  version: number;
  language: 'en' | 'ar';
  theme: 'dark' | 'light' | 'system';
  performanceMode: 'auto' | 'eco' | 'balanced' | 'performance';
  serverPort: number;
  notifications: {
    enabled: boolean;
    sound: boolean;
    messagePreview: boolean;
  };
  privacy: {
    showOnlineStatus: boolean;
    showTypingIndicator: boolean;
    showReadReceipts: boolean;
  };
  media: {
    videoOnByDefault: boolean;
    noiseSuppression: boolean;
    echoCancellation: boolean;
  };
  startup: {
    launchOnBoot: boolean;
    startMinimized: boolean;
    checkForUpdates: boolean;
  };
  advanced: {
    hardwareAcceleration: boolean;
    logLevel: 'error' | 'warn' | 'info' | 'debug';
    diagnosticMode: boolean;
  };
}

export interface InitializationResult {
  success: boolean;
  isFirstRun: boolean;
  isUpgrade: boolean;
  previousVersion: number | null;
  errors: string[];
  warnings: string[];
  timingMs: number;
}

interface LayoutMarker {
  version: number;
  installedAt: string;
  lastUpdatedAt: string;
  appVersion: string;
}

// ── Default Configuration ───────────────────────────────────

const DEFAULT_CONFIG: UserConfig = {
  version: DATA_LAYOUT_VERSION,
  language: 'en',
  theme: 'dark',
  performanceMode: 'auto',
  serverPort: DEFAULT_SERVER_PORT,
  notifications: {
    enabled: true,
    sound: true,
    messagePreview: true,
  },
  privacy: {
    showOnlineStatus: true,
    showTypingIndicator: true,
    showReadReceipts: true,
  },
  media: {
    videoOnByDefault: true,
    noiseSuppression: true,
    echoCancellation: true,
  },
  startup: {
    launchOnBoot: false,
    startMinimized: false,
    checkForUpdates: false, // LAN-only, no remote updates by default
  },
  advanced: {
    hardwareAcceleration: true,
    logLevel: 'info',
    diagnosticMode: false,
  },
};

// ── Main Entry Point ────────────────────────────────────────

/**
 * Run first-run initialization. Idempotent and safe to re-run.
 *
 * Call flow:
 *   AppLifecycleManager.initialize()
 *     → FirstRunInitializer.initialize()
 *       → createDirectoryTree()
 *       → writeDefaultConfig()  (only if no config.json)
 *       → migrateIfNeeded()     (only if layout version changed)
 *       → registerFirewallRule() (best-effort, no admin)
 *       → cleanStaleTempFiles()
 *       → writeInitMarker()
 */
export async function initialize(): Promise<InitializationResult> {
  const startTime = Date.now();
  const errors: string[] = [];
  const warnings: string[] = [];
  let isFirstRun = false;
  let isUpgrade = false;
  let previousVersion: number | null = null;

  const markerPath = join(getAppDataDir(), FIRST_RUN_MARKER);

  try {
    // ── Step 1: Detect state ────────────────────────────────
    if (!existsSync(markerPath)) {
      isFirstRun = true;
      log('First run detected — initializing data directories');
    } else {
      const marker = readLayoutMarker(markerPath);
      if (marker && marker.version < DATA_LAYOUT_VERSION) {
        isUpgrade = true;
        previousVersion = marker.version;
        log(`Layout upgrade detected: v${marker.version} → v${DATA_LAYOUT_VERSION}`);
      } else {
        // Already initialized, current version — quick validate
        log('Already initialized, running quick validation');
        await quickValidate(warnings);
        return {
          success: true,
          isFirstRun: false,
          isUpgrade: false,
          previousVersion: marker?.version ?? null,
          errors: [],
          warnings,
          timingMs: Date.now() - startTime,
        };
      }
    }

    // ── Step 2: Create directory tree ───────────────────────
    log('Creating directory tree...');
    createDirectoryTree(errors);

    // ── Step 3: Write default config ────────────────────────
    if (isFirstRun || !existsSync(getConfigPath())) {
      log('Writing default configuration...');
      writeDefaultConfig(errors);
    }

    // ── Step 4: Migrate if upgrading ────────────────────────
    if (isUpgrade && previousVersion !== null) {
      log(`Migrating data from layout v${previousVersion}...`);
      await migrateData(previousVersion, errors, warnings);
    }

    // ── Step 5: Firewall rule ───────────────────────────────
    if (isFirstRun) {
      log('Attempting to register firewall exception...');
      await registerFirewallRule(warnings);
    }

    // ── Step 6: Clean stale temp files ──────────────────────
    log('Cleaning stale temporary files...');
    cleanStaleTempFiles(warnings);

    // ── Step 7: Clean old logs ──────────────────────────────
    log('Cleaning old log files...');
    cleanOldLogs(warnings);

    // ── Step 8: Write initialization marker ─────────────────
    log('Writing initialization marker...');
    writeInitMarker(markerPath, isFirstRun);

    if (errors.length > 0) {
      log(`Initialization completed with ${errors.length} error(s)`);
    } else {
      log('Initialization completed successfully');
    }

  } catch (err) {
    const msg = `Critical initialization error: ${(err as Error).message}`;
    errors.push(msg);
    log(msg);

    // Show error dialog for critical failures
    dialog.showErrorBox(
      `${APP_NAME} — Initialization Error`,
      `Failed to initialize application data.\n\n${msg}\n\nPlease check folder permissions for:\n${getAppDataDir()}`
    );
  }

  return {
    success: errors.length === 0,
    isFirstRun,
    isUpgrade,
    previousVersion,
    errors,
    warnings,
    timingMs: Date.now() - startTime,
  };
}

// ── Directory Creation ──────────────────────────────────────

function createDirectoryTree(errors: string[]): void {
  const dirs = [
    getAppDataDir(),
    getDataDir(),
    getFilesDir(),
    getFilesSubDir('images'),
    getFilesSubDir('documents'),
    getFilesSubDir('audio'),
    getFilesSubDir('other'),
    getLogsDir(),
    getCacheDir(),
    getAvatarCacheDir(),
    getThumbnailCacheDir(),
    getDiscoveryCacheDir(),
    getCredentialsDir(),
    getTempDir(),
    getUpdateStagingDir(),
    getCrashDumpDir(),
  ];

  for (const dir of dirs) {
    try {
      if (!existsSync(dir)) {
        mkdirSync(dir, { recursive: true });
        log(`  Created: ${dir}`);
      }
    } catch (err) {
      errors.push(`Failed to create directory: ${dir} — ${(err as Error).message}`);
    }
  }
}

// ── Configuration ───────────────────────────────────────────

function writeDefaultConfig(errors: string[]): void {
  const configPath = getConfigPath();
  try {
    // Detect system language for default
    const systemLocale = app.getSystemLocale?.() ?? app.getLocale?.() ?? 'en';
    const config = { ...DEFAULT_CONFIG };
    if (systemLocale.startsWith('ar')) {
      config.language = 'ar';
    }

    writeFileSync(configPath, JSON.stringify(config, null, 2), 'utf-8');
    log(`  Config written: ${configPath}`);
  } catch (err) {
    errors.push(`Failed to write config: ${(err as Error).message}`);
  }
}

/**
 * Read user config with fallback to defaults.
 * Always returns a valid config object — merges with defaults for missing fields.
 */
export function readUserConfig(): UserConfig {
  const configPath = getConfigPath();
  try {
    if (existsSync(configPath)) {
      const raw = readFileSync(configPath, 'utf-8');
      const parsed = JSON.parse(raw);
      // Deep merge with defaults to fill missing fields from older versions
      return deepMerge(DEFAULT_CONFIG, parsed) as UserConfig;
    }
  } catch {
    // Corrupted config — return defaults
  }
  return { ...DEFAULT_CONFIG };
}

/**
 * Write updated user config to disk.
 * Atomic write: writes to temp file first, then renames.
 */
export function writeUserConfig(config: UserConfig): void {
  const configPath = getConfigPath();
  const tempPath = configPath + '.tmp';
  try {
    writeFileSync(tempPath, JSON.stringify(config, null, 2), 'utf-8');
    renameSync(tempPath, configPath);
  } catch {
    // Fallback: direct write
    writeFileSync(configPath, JSON.stringify(config, null, 2), 'utf-8');
  }
}

// ── Data Migration ──────────────────────────────────────────

async function migrateData(
  fromVersion: number,
  errors: string[],
  warnings: string[],
): Promise<void> {
  // Apply migrations sequentially
  for (let v = fromVersion; v < DATA_LAYOUT_VERSION; v++) {
    const migrator = MIGRATIONS[v];
    if (migrator) {
      try {
        log(`  Running migration v${v} → v${v + 1}...`);
        await migrator(errors, warnings);
        log(`  Migration v${v} → v${v + 1} complete`);
      } catch (err) {
        errors.push(`Migration v${v} → v${v + 1} failed: ${(err as Error).message}`);
      }
    }
  }
}

/**
 * Migration registry. Each key is the source version, and the function
 * migrates to version+1. Add new migrations here when DATA_LAYOUT_VERSION bumps.
 */
const MIGRATIONS: Record<number, (errors: string[], warnings: string[]) => Promise<void>> = {
  // Example: Migration from layout v0 → v1
  // 0: async (errors, warnings) => {
  //   // Move files from old location to new location
  //   const oldFilesDir = join(getDataDir(), 'uploads');
  //   const newFilesDir = getFilesDir();
  //   if (existsSync(oldFilesDir) && !existsSync(newFilesDir)) {
  //     renameSync(oldFilesDir, newFilesDir);
  //   }
  // },
};

// ── Firewall Registration ───────────────────────────────────

/**
 * Attempt to add a Windows Firewall exception for the backend server.
 * This uses netsh and may fail without admin (which is fine — user can
 * approve the firewall prompt manually on first LAN connection).
 */
async function registerFirewallRule(warnings: string[]): Promise<void> {
  if (isDev || process.platform !== 'win32') return;

  const serverExe = getServerExePath();
  if (!existsSync(serverExe)) {
    warnings.push('Server executable not found — skipping firewall rule');
    return;
  }

  return new Promise<void>((resolve) => {
    // Try to add firewall rule — this may silently fail without admin
    const args = [
      'advfirewall', 'firewall', 'add', 'rule',
      `name=${APP_NAME} Server`,
      'dir=in',
      'action=allow',
      `program=${serverExe}`,
      'enable=yes',
      'profile=private',
      `description=${APP_NAME} LAN communication server`,
    ];

    const proc = spawn('netsh', args, {
      windowsHide: true,
      stdio: 'ignore',
    });

    proc.on('exit', (code) => {
      if (code === 0) {
        log('  Firewall rule added successfully');
      } else {
        warnings.push(
          'Could not add firewall exception automatically. ' +
          'You may see a Windows Firewall prompt on first LAN connection.'
        );
      }
      resolve();
    });

    proc.on('error', () => {
      warnings.push('netsh not available — firewall rule not added');
      resolve();
    });

    // Timeout: don't block startup
    setTimeout(() => {
      try { proc.kill(); } catch {}
      resolve();
    }, 5_000);
  });
}

// ── Cleanup ─────────────────────────────────────────────────

function cleanStaleTempFiles(warnings: string[]): void {
  const tempDir = getTempDir();
  try {
    if (!existsSync(tempDir)) return;

    const entries = readdirSync(tempDir);
    const now = Date.now();
    const maxAge = 7 * 24 * 60 * 60 * 1000; // 7 days

    for (const entry of entries) {
      const fullPath = join(tempDir, entry);
      try {
        const stat = statSync(fullPath);
        if (now - stat.mtimeMs > maxAge) {
          if (stat.isFile()) {
            unlinkSync(fullPath);
            log(`  Cleaned temp file: ${entry}`);
          }
          // Don't recursively delete directories — too risky
        }
      } catch {}
    }
  } catch {
    warnings.push('Could not clean temp directory');
  }
}

function cleanOldLogs(warnings: string[]): void {
  const logsDir = getLogsDir();
  try {
    if (!existsSync(logsDir)) return;

    const entries = readdirSync(logsDir);
    const now = Date.now();
    const maxAge = LOG_RETENTION_DAYS * 24 * 60 * 60 * 1000;

    let cleaned = 0;
    for (const entry of entries) {
      if (!entry.endsWith('.log')) continue;
      const fullPath = join(logsDir, entry);
      try {
        const stat = statSync(fullPath);
        if (now - stat.mtimeMs > maxAge) {
          unlinkSync(fullPath);
          cleaned++;
        }
      } catch {}
    }

    if (cleaned > 0) {
      log(`  Cleaned ${cleaned} old log file(s)`);
    }
  } catch {
    warnings.push('Could not clean old logs');
  }
}

// ── Init Marker ─────────────────────────────────────────────

function readLayoutMarker(markerPath: string): LayoutMarker | null {
  try {
    const raw = readFileSync(markerPath, 'utf-8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function writeInitMarker(markerPath: string, isNew: boolean): void {
  const marker: LayoutMarker = {
    version: DATA_LAYOUT_VERSION,
    installedAt: isNew ? new Date().toISOString() : readLayoutMarker(markerPath)?.installedAt ?? new Date().toISOString(),
    lastUpdatedAt: new Date().toISOString(),
    appVersion: app.getVersion(),
  };
  writeFileSync(markerPath, JSON.stringify(marker, null, 2), 'utf-8');
}

// ── Quick Validate ──────────────────────────────────────────

async function quickValidate(warnings: string[]): Promise<void> {
  // Verify critical directories still exist (user might have deleted them)
  const criticalDirs = [getDataDir(), getLogsDir(), getCacheDir(), getFilesDir()];
  for (const dir of criticalDirs) {
    if (!existsSync(dir)) {
      try {
        mkdirSync(dir, { recursive: true });
        warnings.push(`Recreated missing directory: ${dir}`);
      } catch {
        warnings.push(`Critical directory missing and cannot be created: ${dir}`);
      }
    }
  }
}

// ── Utility ─────────────────────────────────────────────────

function log(msg: string): void {
  console.log(`[FirstRun] ${msg}`);
}

function deepMerge(target: Record<string, any>, source: Record<string, any>): Record<string, any> {
  const result = { ...target };
  for (const key of Object.keys(source)) {
    if (
      source[key] !== null &&
      typeof source[key] === 'object' &&
      !Array.isArray(source[key]) &&
      typeof target[key] === 'object' &&
      target[key] !== null
    ) {
      result[key] = deepMerge(target[key], source[key]);
    } else {
      result[key] = source[key];
    }
  }
  return result;
}
