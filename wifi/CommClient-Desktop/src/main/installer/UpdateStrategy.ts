/**
 * UpdateStrategy.ts — Update-safe file strategy, version migration, and rollback.
 *
 * Handles the LAN-only update model for CommClient:
 *
 * Since this is a LAN-only application with NO internet access, updates
 * are distributed via:
 *   1. Local file share (\\server\updates\CommClient-Setup-x.y.z.exe)
 *   2. USB drive (D:\CommClient-Setup-x.y.z.exe)
 *   3. Admin-pushed silent install (/S flag)
 *
 * Update-safe file categories:
 * ┌───────────────────────┬──────────────────────────────────────────────────┐
 * │ Category              │ Strategy                                         │
 * ├───────────────────────┼──────────────────────────────────────────────────┤
 * │ App binaries          │ REPLACED by installer (in %LOCALAPPDATA%)        │
 * │ Electron/Node DLLs    │ REPLACED by installer                            │
 * │ app.asar              │ REPLACED by installer (contains renderer+main)   │
 * │ Server bundle         │ REPLACED by installer (PyInstaller output)       │
 * │ User config           │ PRESERVED — merged with new defaults             │
 * │ SQLite database       │ PRESERVED — migrations run on first launch       │
 * │ Uploaded files         │ PRESERVED — never touched by installer           │
 * │ Logs                  │ PRESERVED — old logs cleaned by retention policy  │
 * │ Cache                 │ CLEARED — stale cache can cause issues            │
 * │ Temp files            │ CLEARED — ephemeral by definition                │
 * │ Credentials           │ PRESERVED — DPAPI-encrypted session data         │
 * │ Window state          │ PRESERVED — user preference                      │
 * └───────────────────────┴──────────────────────────────────────────────────┘
 *
 * The NSIS installer handles:
 *   - Detecting running instance and prompting to close
 *   - Replacing files in the install directory (read-only zone)
 *   - NOT touching %APPDATA%\CommClient (user data zone)
 *
 * This module handles:
 *   - Post-update first-launch migration
 *   - Config schema migration (old config → new defaults)
 *   - Database schema migration markers
 *   - Cache invalidation after update
 *   - Rollback preparation (backup before migration)
 *   - Version comparison and compatibility checks
 */

import { app } from 'electron';
import { join } from 'path';
import {
  existsSync,
  readFileSync,
  writeFileSync,
  copyFileSync,
  mkdirSync,
  readdirSync,
  unlinkSync,
  rmSync,
  statSync,
  renameSync,
} from 'fs';
import {
  getAppDataDir,
  getCacheDir,
  getConfigPath,
  getDatabasePath,
  DATA_LAYOUT_VERSION,
} from './InstallerConfig';
import { readUserConfig, writeUserConfig, type UserConfig } from './FirstRunInitializer';

// ── Types ───────────────────────────────────────────────────

export interface VersionInfo {
  major: number;
  minor: number;
  patch: number;
  raw: string;
}

export interface UpdateCheckResult {
  updateAvailable: boolean;
  currentVersion: VersionInfo;
  previousVersion: VersionInfo | null;
  requiresMigration: boolean;
  requiresCacheClear: boolean;
}

export interface MigrationResult {
  success: boolean;
  configMigrated: boolean;
  cacheCleaned: boolean;
  backupPath: string | null;
  errors: string[];
}

// ── Version Tracking ────────────────────────────────────────

const VERSION_FILE = 'last-version.json';

interface VersionRecord {
  version: string;
  installedAt: string;
  updatedAt: string;
  layoutVersion: number;
}

/**
 * Parse a semver string into components.
 */
export function parseVersion(versionStr: string): VersionInfo {
  const clean = versionStr.replace(/^v/, '');
  const parts = clean.split('.').map(Number);
  return {
    major: parts[0] || 0,
    minor: parts[1] || 0,
    patch: parts[2] || 0,
    raw: clean,
  };
}

/**
 * Compare two versions. Returns:
 *   -1 if a < b
 *    0 if a === b
 *    1 if a > b
 */
export function compareVersions(a: VersionInfo, b: VersionInfo): -1 | 0 | 1 {
  if (a.major !== b.major) return a.major < b.major ? -1 : 1;
  if (a.minor !== b.minor) return a.minor < b.minor ? -1 : 1;
  if (a.patch !== b.patch) return a.patch < b.patch ? -1 : 1;
  return 0;
}

/**
 * Check if an update was just installed (current app version differs from saved version).
 */
export function checkForUpdate(): UpdateCheckResult {
  const currentVersion = parseVersion(app.getVersion());
  const versionFilePath = join(getAppDataDir(), VERSION_FILE);

  let previousVersion: VersionInfo | null = null;
  let previousRecord: VersionRecord | null = null;

  try {
    if (existsSync(versionFilePath)) {
      const raw = readFileSync(versionFilePath, 'utf-8');
      previousRecord = JSON.parse(raw);
      if (previousRecord) {
        previousVersion = parseVersion(previousRecord.version);
      }
    }
  } catch {}

  const updateAvailable = previousVersion !== null
    && compareVersions(currentVersion, previousVersion) > 0;

  // Migration required if major or minor version changed
  const requiresMigration = updateAvailable && previousVersion !== null
    && (currentVersion.major !== previousVersion.major || currentVersion.minor !== previousVersion.minor);

  // Cache clear on any version change
  const requiresCacheClear = updateAvailable;

  return {
    updateAvailable,
    currentVersion,
    previousVersion,
    requiresMigration,
    requiresCacheClear,
  };
}

/**
 * Record the current version after successful startup.
 * Called at the end of AppLifecycleManager.start().
 */
export function recordCurrentVersion(): void {
  const versionFilePath = join(getAppDataDir(), VERSION_FILE);
  const currentVersion = app.getVersion();

  let record: VersionRecord;
  try {
    if (existsSync(versionFilePath)) {
      const existing = JSON.parse(readFileSync(versionFilePath, 'utf-8'));
      record = {
        version: currentVersion,
        installedAt: existing.installedAt || new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        layoutVersion: DATA_LAYOUT_VERSION,
      };
    } else {
      record = {
        version: currentVersion,
        installedAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        layoutVersion: DATA_LAYOUT_VERSION,
      };
    }
  } catch {
    record = {
      version: currentVersion,
      installedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      layoutVersion: DATA_LAYOUT_VERSION,
    };
  }

  writeFileSync(versionFilePath, JSON.stringify(record, null, 2), 'utf-8');
}

// ── Post-Update Migration ───────────────────────────────────

/**
 * Run all post-update tasks. Called from AppLifecycleManager when
 * checkForUpdate().updateAvailable is true.
 */
export async function runPostUpdateMigration(): Promise<MigrationResult> {
  const errors: string[] = [];
  let configMigrated = false;
  let cacheCleaned = false;
  let backupPath: string | null = null;

  const check = checkForUpdate();

  try {
    // ── Step 1: Create backup before any changes ────────
    if (check.requiresMigration) {
      backupPath = createPreMigrationBackup(errors);
    }

    // ── Step 2: Migrate config ──────────────────────────
    if (check.requiresMigration) {
      configMigrated = migrateConfig(check.previousVersion!, check.currentVersion, errors);
    }

    // ── Step 3: Clear stale cache ───────────────────────
    if (check.requiresCacheClear) {
      cacheCleaned = clearCache(errors);
    }

    // ── Step 4: Record new version ──────────────────────
    recordCurrentVersion();

  } catch (err) {
    errors.push(`Post-update migration failed: ${(err as Error).message}`);
  }

  return {
    success: errors.length === 0,
    configMigrated,
    cacheCleaned,
    backupPath,
    errors,
  };
}

// ── Backup ──────────────────────────────────────────────────

/**
 * Create a backup of critical user data before migration.
 * Stores in %APPDATA%\CommClient\backups\pre-update-<version>\
 */
function createPreMigrationBackup(errors: string[]): string | null {
  const version = app.getVersion();
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const backupDir = join(getAppDataDir(), 'backups', `pre-update-${version}-${timestamp}`);

  try {
    mkdirSync(backupDir, { recursive: true });

    // Backup config
    const configPath = getConfigPath();
    if (existsSync(configPath)) {
      copyFileSync(configPath, join(backupDir, 'config.json'));
    }

    // Backup database (copy the .db file — WAL will be checkpointed on shutdown)
    const dbPath = getDatabasePath();
    if (existsSync(dbPath)) {
      copyFileSync(dbPath, join(backupDir, 'commclient.db'));
    }

    // Backup version record
    const versionFile = join(getAppDataDir(), VERSION_FILE);
    if (existsSync(versionFile)) {
      copyFileSync(versionFile, join(backupDir, VERSION_FILE));
    }

    log(`Backup created at: ${backupDir}`);
    return backupDir;
  } catch (err) {
    errors.push(`Backup failed: ${(err as Error).message}`);
    return null;
  }
}

/**
 * Restore from a backup after a failed migration.
 */
export function rollbackFromBackup(backupDir: string): boolean {
  try {
    // Restore config
    const backupConfig = join(backupDir, 'config.json');
    if (existsSync(backupConfig)) {
      copyFileSync(backupConfig, getConfigPath());
    }

    // Note: DB rollback is risky — don't auto-restore the DB
    // as it may have been written to since the backup.
    // The user can manually restore from the backup folder.

    log(`Rolled back config from: ${backupDir}`);
    return true;
  } catch (err) {
    log(`Rollback failed: ${(err as Error).message}`);
    return false;
  }
}

/**
 * Clean old backup directories, keeping only the last 3.
 */
export function cleanOldBackups(): void {
  const backupsDir = join(getAppDataDir(), 'backups');
  if (!existsSync(backupsDir)) return;

  try {
    const entries = readdirSync(backupsDir)
      .map(name => ({ name, mtime: statSync(join(backupsDir, name)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime); // newest first

    // Keep only the last 3 backups
    for (let i = 3; i < entries.length; i++) {
      const fullPath = join(backupsDir, entries[i].name);
      try {
        rmSync(fullPath, { recursive: true, force: true });
        log(`Cleaned old backup: ${entries[i].name}`);
      } catch {}
    }
  } catch {}
}

// ── Config Migration ────────────────────────────────────────

/**
 * Migrate user config from old version to new version.
 * Strategy: read existing config, merge with new defaults, preserve user values.
 */
function migrateConfig(
  from: VersionInfo,
  to: VersionInfo,
  errors: string[],
): boolean {
  try {
    const currentConfig = readUserConfig();
    // readUserConfig already merges with defaults, so new fields get default values
    // Just update the version number
    currentConfig.version = DATA_LAYOUT_VERSION;
    writeUserConfig(currentConfig);
    log(`Config migrated from ${from.raw} to ${to.raw}`);
    return true;
  } catch (err) {
    errors.push(`Config migration failed: ${(err as Error).message}`);
    return false;
  }
}

// ── Cache Invalidation ──────────────────────────────────────

/**
 * Clear all cached data after an update.
 * Caches may contain stale data incompatible with the new version.
 */
function clearCache(errors: string[]): boolean {
  const cacheDir = getCacheDir();
  if (!existsSync(cacheDir)) return true;

  try {
    const subdirs = ['avatars', 'thumbnails', 'discovery'];
    for (const sub of subdirs) {
      const subDir = join(cacheDir, sub);
      if (existsSync(subDir)) {
        try {
          const files = readdirSync(subDir);
          for (const file of files) {
            try { unlinkSync(join(subDir, file)); } catch {}
          }
          log(`  Cleared cache: ${sub} (${files.length} files)`);
        } catch {}
      }
    }
    return true;
  } catch (err) {
    errors.push(`Cache clear failed: ${(err as Error).message}`);
    return false;
  }
}

// ── Silent Install Support ──────────────────────────────────

/**
 * Configuration for admin-pushed silent updates on LAN.
 * When the NSIS installer is run with /S, these registry values
 * are checked to determine behavior.
 *
 * Silent install command:
 *   CommClient-Setup-1.2.0.exe /S /D=C:\Users\<user>\AppData\Local\Programs\CommClient
 *
 * Silent uninstall command:
 *   "%LOCALAPPDATA%\Programs\CommClient\Uninstall CommClient.exe" /S
 */
export const SILENT_INSTALL_FLAGS = {
  /** Run installer silently (no UI) */
  silent: '/S',
  /** Specify install directory */
  installDir: '/D=',
  /** Skip firewall prompt */
  noFirewall: '/NOFIREWALL',
  /** Keep user data during uninstall */
  keepData: '/KEEPDATA',
} as const;

/**
 * Generate a silent install command for LAN deployment.
 */
export function generateSilentInstallCommand(
  setupPath: string,
  installDir?: string,
): string {
  let cmd = `"${setupPath}" /S`;
  if (installDir) {
    cmd += ` /D=${installDir}`;
  }
  return cmd;
}

/**
 * Generate a silent uninstall command.
 */
export function generateSilentUninstallCommand(installDir: string): string {
  return `"${join(installDir, `Uninstall ${getAppName()}.exe`)}" /S`;
}

function getAppName(): string {
  return 'CommClient';
}

// ── Utility ─────────────────────────────────────────────────

function log(msg: string): void {
  console.log(`[UpdateStrategy] ${msg}`);
}
