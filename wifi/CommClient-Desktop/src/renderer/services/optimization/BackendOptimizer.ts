/**
 * BackendOptimizer.ts — Backend performance tuning configuration.
 *
 * This module provides runtime configuration for the Python/FastAPI backend
 * that runs locally alongside the Electron app. It generates optimal
 * SQLite, FastAPI, and Socket.IO configurations based on the detected
 * device capability tier.
 *
 * Optimizations:
 *   1. SQLite WAL mode for concurrent reads during writes
 *   2. Query batching configuration for bulk message operations
 *   3. Connection pool sizing based on available resources
 *   4. API response compression (gzip/br) for large payloads
 *   5. Socket.IO transport optimization (WebSocket preferred on LAN)
 *   6. File upload chunking configuration
 *   7. Background task scheduling (message cleanup, index maintenance)
 *
 * This module is consumed by the Electron main process, which passes
 * these configs to the Python backend at startup via environment variables
 * or a config file.
 */

import type { DeviceTier } from '../performance/DeviceCapabilityDetector';

// ── Types ───────────────────────────────────────────────────

export interface SQLiteConfig {
  /** WAL mode for concurrent access */
  journalMode: 'wal' | 'delete' | 'truncate';
  /** Page size (bytes) — larger = better for large reads */
  pageSize: number;
  /** Cache size (pages, negative = KiB) */
  cacheSize: number;
  /** Synchronous level */
  synchronous: 'OFF' | 'NORMAL' | 'FULL';
  /** Memory-mapped I/O size (bytes, 0 = disabled) */
  mmapSize: number;
  /** Temp store location */
  tempStore: 'DEFAULT' | 'FILE' | 'MEMORY';
  /** Auto-vacuum mode */
  autoVacuum: 'NONE' | 'FULL' | 'INCREMENTAL';
  /** Busy timeout (ms) */
  busyTimeoutMs: number;
  /** Max page count (0 = unlimited) */
  maxPageCount: number;
}

export interface APIConfig {
  /** Number of worker threads for uvicorn */
  workers: number;
  /** Request timeout (seconds) */
  requestTimeoutSec: number;
  /** Max request body size (bytes) */
  maxBodySize: number;
  /** Enable response compression */
  compression: boolean;
  /** Compression level (1-9) */
  compressionLevel: number;
  /** Max concurrent WebSocket connections */
  maxWebsockets: number;
  /** API rate limit (requests per minute per user) */
  rateLimitPerMinute: number;
  /** Enable request logging */
  accessLog: boolean;
}

export interface SocketIOConfig {
  /** Transport preference order */
  transports: ('websocket' | 'polling')[];
  /** Ping timeout (ms) */
  pingTimeoutMs: number;
  /** Ping interval (ms) */
  pingIntervalMs: number;
  /** Max buffer size (bytes) */
  maxBufferSize: number;
  /** Allow upgrade from polling to websocket */
  allowUpgrades: boolean;
  /** Per-message deflate compression */
  perMessageDeflate: boolean;
  /** Max HTTP buffer size for polling */
  maxHttpBufferSize: number;
}

export interface QueryBatchConfig {
  /** Maximum messages to fetch in one query */
  maxMessageBatchSize: number;
  /** Maximum channels to sync in one request */
  maxChannelSyncBatch: number;
  /** Batch insert size for message imports */
  batchInsertSize: number;
  /** Search result page size */
  searchPageSize: number;
  /** Maximum concurrent database queries */
  maxConcurrentQueries: number;
}

export interface BackgroundTaskConfig {
  /** Message cleanup interval (minutes) */
  messageCleanupIntervalMin: number;
  /** Maximum message age before cleanup (days, 0 = never) */
  messageMaxAgeDays: number;
  /** Database VACUUM interval (hours, 0 = never) */
  vacuumIntervalHours: number;
  /** Index optimization interval (hours) */
  indexOptimizeIntervalHours: number;
  /** Session cleanup interval (minutes) */
  sessionCleanupIntervalMin: number;
  /** File cache cleanup interval (minutes) */
  fileCacheCleanupIntervalMin: number;
}

export interface FileTransferConfig {
  /** Upload chunk size (bytes) */
  uploadChunkSize: number;
  /** Maximum file size (bytes) */
  maxFileSize: number;
  /** Maximum concurrent uploads */
  maxConcurrentUploads: number;
  /** Maximum concurrent downloads */
  maxConcurrentDownloads: number;
  /** Enable resumable uploads */
  resumableUploads: boolean;
  /** File cache max size (MB) */
  fileCacheMaxMB: number;
}

export interface BackendConfig {
  sqlite: SQLiteConfig;
  api: APIConfig;
  socketio: SocketIOConfig;
  queryBatch: QueryBatchConfig;
  backgroundTasks: BackgroundTaskConfig;
  fileTransfer: FileTransferConfig;
}

// ── Configuration Generators ────────────────────────────────

function getSQLiteConfig(tier: DeviceTier): SQLiteConfig {
  const base: SQLiteConfig = {
    journalMode: 'wal',
    pageSize: 4096,
    cacheSize: -2000,        // 2MB
    synchronous: 'NORMAL',
    mmapSize: 0,
    tempStore: 'MEMORY',
    autoVacuum: 'INCREMENTAL',
    busyTimeoutMs: 5000,
    maxPageCount: 0,
  };

  switch (tier) {
    case 'minimal':
      base.cacheSize = -1000;     // 1MB
      base.synchronous = 'NORMAL';
      base.mmapSize = 0;
      base.busyTimeoutMs = 3000;
      break;
    case 'low':
      base.cacheSize = -2000;     // 2MB
      base.mmapSize = 0;
      break;
    case 'medium':
      base.cacheSize = -8000;     // 8MB
      base.mmapSize = 64 * 1024 * 1024;  // 64MB mmap
      base.pageSize = 8192;
      break;
    case 'high':
      base.cacheSize = -32000;    // 32MB
      base.mmapSize = 256 * 1024 * 1024;  // 256MB mmap
      base.pageSize = 8192;
      base.synchronous = 'NORMAL';
      break;
  }

  return base;
}

function getAPIConfig(tier: DeviceTier): APIConfig {
  return {
    workers: tier === 'high' ? 2 : 1,  // Single worker for most, 2 for high-end
    requestTimeoutSec: tier === 'minimal' ? 30 : 15,
    maxBodySize: 50 * 1024 * 1024,  // 50MB max file upload
    compression: tier !== 'minimal',
    compressionLevel: tier === 'high' ? 6 : tier === 'medium' ? 4 : 1,
    maxWebsockets: tier === 'high' ? 50 : tier === 'medium' ? 30 : 15,
    rateLimitPerMinute: 300,
    accessLog: false,  // Disable in production (saves I/O)
  };
}

function getSocketIOConfig(tier: DeviceTier): SocketIOConfig {
  return {
    transports: ['websocket'],  // LAN: always prefer WebSocket
    pingTimeoutMs: tier === 'minimal' ? 10_000 : 5_000,
    pingIntervalMs: tier === 'minimal' ? 30_000 : tier === 'low' ? 15_000 : 10_000,
    maxBufferSize: 10 * 1024 * 1024,   // 10MB
    allowUpgrades: true,
    perMessageDeflate: tier !== 'minimal',  // Compression costs CPU
    maxHttpBufferSize: 5 * 1024 * 1024,
  };
}

function getQueryBatchConfig(tier: DeviceTier): QueryBatchConfig {
  return {
    maxMessageBatchSize: tier === 'high' ? 100 : tier === 'medium' ? 50 : 30,
    maxChannelSyncBatch: tier === 'high' ? 20 : 10,
    batchInsertSize: tier === 'high' ? 500 : tier === 'medium' ? 200 : 100,
    searchPageSize: tier === 'high' ? 50 : 25,
    maxConcurrentQueries: tier === 'high' ? 4 : tier === 'medium' ? 2 : 1,
  };
}

function getBackgroundTaskConfig(tier: DeviceTier): BackgroundTaskConfig {
  return {
    messageCleanupIntervalMin: tier === 'minimal' ? 60 : 30,
    messageMaxAgeDays: 0,  // Never auto-delete
    vacuumIntervalHours: tier === 'minimal' ? 48 : 24,
    indexOptimizeIntervalHours: tier === 'minimal' ? 72 : 24,
    sessionCleanupIntervalMin: 15,
    fileCacheCleanupIntervalMin: tier === 'minimal' ? 30 : 60,
  };
}

function getFileTransferConfig(tier: DeviceTier): FileTransferConfig {
  return {
    uploadChunkSize: tier === 'high' ? 512 * 1024 : tier === 'medium' ? 256 * 1024 : 128 * 1024,
    maxFileSize: 100 * 1024 * 1024,  // 100MB
    maxConcurrentUploads: tier === 'high' ? 3 : tier === 'medium' ? 2 : 1,
    maxConcurrentDownloads: tier === 'high' ? 5 : tier === 'medium' ? 3 : 2,
    resumableUploads: true,
    fileCacheMaxMB: tier === 'high' ? 500 : tier === 'medium' ? 200 : 50,
  };
}

// ── Public API ──────────────────────────────────────────────

/**
 * Generate complete backend configuration for the detected device tier.
 */
export function generateBackendConfig(tier: DeviceTier): BackendConfig {
  return {
    sqlite: getSQLiteConfig(tier),
    api: getAPIConfig(tier),
    socketio: getSocketIOConfig(tier),
    queryBatch: getQueryBatchConfig(tier),
    backgroundTasks: getBackgroundTaskConfig(tier),
    fileTransfer: getFileTransferConfig(tier),
  };
}

/**
 * Generate SQLite PRAGMA statements from config.
 * These are executed at database connection startup.
 */
export function generateSQLitePragmas(config: SQLiteConfig): string[] {
  return [
    `PRAGMA journal_mode=${config.journalMode};`,
    `PRAGMA page_size=${config.pageSize};`,
    `PRAGMA cache_size=${config.cacheSize};`,
    `PRAGMA synchronous=${config.synchronous};`,
    `PRAGMA mmap_size=${config.mmapSize};`,
    `PRAGMA temp_store=${config.tempStore === 'MEMORY' ? 2 : config.tempStore === 'FILE' ? 1 : 0};`,
    `PRAGMA auto_vacuum=${config.autoVacuum === 'FULL' ? 1 : config.autoVacuum === 'INCREMENTAL' ? 2 : 0};`,
    `PRAGMA busy_timeout=${config.busyTimeoutMs};`,
    config.maxPageCount > 0 ? `PRAGMA max_page_count=${config.maxPageCount};` : '',
    // Performance pragmas always enabled
    'PRAGMA foreign_keys=ON;',
    'PRAGMA recursive_triggers=ON;',
  ].filter(Boolean);
}

/**
 * Generate a JSON config file content for the Python backend.
 */
export function generateBackendConfigJSON(tier: DeviceTier): string {
  const config = generateBackendConfig(tier);
  return JSON.stringify(config, null, 2);
}

/**
 * Generate environment variables for the Python backend.
 */
export function generateBackendEnvVars(tier: DeviceTier): Record<string, string> {
  const config = generateBackendConfig(tier);

  return {
    // SQLite
    CC_SQLITE_JOURNAL_MODE: config.sqlite.journalMode,
    CC_SQLITE_CACHE_SIZE: String(config.sqlite.cacheSize),
    CC_SQLITE_SYNCHRONOUS: config.sqlite.synchronous,
    CC_SQLITE_MMAP_SIZE: String(config.sqlite.mmapSize),
    CC_SQLITE_PAGE_SIZE: String(config.sqlite.pageSize),
    CC_SQLITE_BUSY_TIMEOUT: String(config.sqlite.busyTimeoutMs),

    // API
    CC_API_WORKERS: String(config.api.workers),
    CC_API_REQUEST_TIMEOUT: String(config.api.requestTimeoutSec),
    CC_API_MAX_BODY_SIZE: String(config.api.maxBodySize),
    CC_API_COMPRESSION: String(config.api.compression),
    CC_API_COMPRESSION_LEVEL: String(config.api.compressionLevel),
    CC_API_MAX_WEBSOCKETS: String(config.api.maxWebsockets),

    // Socket.IO
    CC_SOCKETIO_PING_TIMEOUT: String(config.socketio.pingTimeoutMs),
    CC_SOCKETIO_PING_INTERVAL: String(config.socketio.pingIntervalMs),
    CC_SOCKETIO_PER_MESSAGE_DEFLATE: String(config.socketio.perMessageDeflate),

    // Query batching
    CC_QUERY_MAX_MSG_BATCH: String(config.queryBatch.maxMessageBatchSize),
    CC_QUERY_MAX_CHANNEL_SYNC: String(config.queryBatch.maxChannelSyncBatch),
    CC_QUERY_BATCH_INSERT_SIZE: String(config.queryBatch.batchInsertSize),
    CC_QUERY_MAX_CONCURRENT: String(config.queryBatch.maxConcurrentQueries),

    // File transfer
    CC_FILE_CHUNK_SIZE: String(config.fileTransfer.uploadChunkSize),
    CC_FILE_MAX_SIZE: String(config.fileTransfer.maxFileSize),
    CC_FILE_MAX_CONCURRENT_UP: String(config.fileTransfer.maxConcurrentUploads),
    CC_FILE_MAX_CONCURRENT_DOWN: String(config.fileTransfer.maxConcurrentDownloads),
    CC_FILE_CACHE_MAX_MB: String(config.fileTransfer.fileCacheMaxMB),

    // Device tier
    CC_DEVICE_TIER: tier,
  };
}
