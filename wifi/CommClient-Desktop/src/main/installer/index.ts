/**
 * installer/ — Phase 12: Windows Installer Engineering
 *
 * ┌───────────────────────────────────────────────────────────────────┐
 * │                CommClient Installer Architecture                  │
 * │                                                                   │
 * │  ┌──────────────┐     ┌───────────────────┐                      │
 * │  │ NSIS Script   │     │ electron-builder  │                      │
 * │  │ installer.nsh │────▶│ electron-builder  │                      │
 * │  │ • Kill procs  │     │ .yml              │                      │
 * │  │ • Firewall    │     │ • ASAR + server   │                      │
 * │  │ • VCRedist    │     │ • Per-user NSIS   │                      │
 * │  │ • User data   │     │ • Shortcuts       │                      │
 * │  └──────────────┘     └─────────┬─────────┘                      │
 * │                                  │ builds                         │
 * │                                  ▼                                │
 * │  ┌─────────────────────────────────────────────────────────┐     │
 * │  │ CommClient Setup x.y.z.exe                              │     │
 * │  │ Installs to: %LOCALAPPDATA%\Programs\CommClient         │     │
 * │  └─────────────────────────────────────────────────────────┘     │
 * │                                  │ runs                           │
 * │                                  ▼                                │
 * │  ┌─────────────────┐   ┌──────────────────┐                     │
 * │  │ InstallerConfig  │   │ AppLifecycle     │                     │
 * │  │ • All paths      │──▶│ Manager          │                     │
 * │  │ • Env detection  │   │ • Start sequence │                     │
 * │  │ • Server env     │   │ • Server launch  │                     │
 * │  │ • Diagnostics    │   │ • Window create  │                     │
 * │  └─────────────────┘   │ • Shutdown       │                     │
 * │                         └───────┬──────────┘                     │
 * │                                 │ first run?                     │
 * │                                 ▼                                │
 * │  ┌─────────────────┐   ┌──────────────────┐                     │
 * │  │ FirstRun         │   │ UpdateStrategy   │                     │
 * │  │ Initializer      │   │ • Version check  │                     │
 * │  │ • Create dirs    │   │ • Config migrate │                     │
 * │  │ • Default config │   │ • Cache clear    │                     │
 * │  │ • Firewall rule  │   │ • Backup/rollback│                     │
 * │  │ • Log cleanup    │   │ • Silent deploy  │                     │
 * │  └─────────────────┘   └──────────────────┘                     │
 * └───────────────────────────────────────────────────────────────────┘
 */

// ── InstallerConfig ─────────────────────────────────────────
export {
  APP_NAME,
  APP_ID,
  SERVER_EXE_NAME,
  DEFAULT_SERVER_PORT,
  SERVER_HEALTH_TIMEOUT_MS,
  SERVER_SHUTDOWN_TIMEOUT_MS,
  LOG_RETENTION_DAYS,
  MAX_LOG_SIZE_MB,
  DB_FILE_NAME,
  CONFIG_FILE_NAME,
  FIRST_RUN_MARKER,
  DATA_LAYOUT_VERSION,
  isDev,
  isPortable,
  isFirstRun,
  getInstallDir,
  getResourcesDir,
  getAppDataDir,
  getDataDir,
  getLogsDir,
  getCacheDir,
  getCredentialsDir,
  getConfigPath,
  getDatabasePath,
  getFilesDir,
  getFilesSubDir,
  getAvatarCacheDir,
  getThumbnailCacheDir,
  getDiscoveryCacheDir,
  getTempDir,
  getUpdateStagingDir,
  getCrashDumpDir,
  getServerExePath,
  isServerBundled,
  getServerEnv,
  getAppIconPath,
  getTrayIconPath,
  getPreloadPath,
  getLogFilePath,
  getServerLogPath,
  getMainLogPath,
  validatePaths,
  getPathDiagnostics,
} from './InstallerConfig';

// ── FirstRunInitializer ─────────────────────────────────────
export {
  initialize as firstRunInitialize,
  readUserConfig,
  writeUserConfig,
  type UserConfig,
  type InitializationResult,
} from './FirstRunInitializer';

// ── AppLifecycleManager ─────────────────────────────────────
export {
  appLifecycle,
  type LifecyclePhase,
  type LifecycleState,
} from './AppLifecycleManager';

// ── UpdateStrategy ──────────────────────────────────────────
export {
  parseVersion,
  compareVersions,
  checkForUpdate,
  recordCurrentVersion,
  runPostUpdateMigration,
  rollbackFromBackup,
  cleanOldBackups,
  generateSilentInstallCommand,
  generateSilentUninstallCommand,
  SILENT_INSTALL_FLAGS,
  type VersionInfo,
  type UpdateCheckResult,
  type MigrationResult,
} from './UpdateStrategy';
