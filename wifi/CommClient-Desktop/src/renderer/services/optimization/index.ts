/**
 * Optimization subsystem — barrel exports.
 *
 * Phase 10: Deep performance optimization layer.
 *
 * Architecture:
 *
 *   StartupOptimizer ───────┐
 *     (cold-start accel)    │
 *                           │
 *   MemoryManager ──────────┤
 *     (heap / cache / pool) │
 *                           │
 *   SocketOptimizer ────────┤──▶ Application Layer
 *     (batch / delta / hb)  │
 *                           │
 *   MediaPipeline ──────────┤
 *     (codec / HW accel)    │
 *                           │
 *   ScreenShareOptimizer ───┤
 *     (frame diff / FPS)    │
 *                           │
 *   UIRenderEngine ─────────┤
 *     (memoize / schedule)  │
 *                           │
 *   BackendOptimizer ───────┤
 *     (SQLite / API config) │
 *                           │
 *   GroupCallResourceMgr ───┘
 *     (stagger / ICE batch)
 */

// Startup acceleration
export {
  StartupOptimizer,
  startupOptimizer,
  type StartupTiming,
  type PreloadHint,
} from './StartupOptimizer';

// Memory management
export {
  MemoryManager,
  memoryManager,
  LRUCache,
  ObjectPool,
  AudioContextPool,
  BlobRegistry,
  SubscriptionTracker,
  HeapMonitor,
  type HeapSnapshot,
  type HeapPressureLevel,
  type SubscriptionRecord,
} from './MemoryManager';

// Socket traffic optimization
export {
  SocketOptimizer,
  type SocketPriority,
  type BatchConfig,
  type SocketTrafficStats,
} from './SocketOptimizer';

// Media pipeline optimization
export {
  MediaPipeline,
  mediaPipeline,
  type VideoCodec,
  type AudioCodec,
  type CodecCapability,
  type AudioPipelineConfig,
  type VideoPipelineConfig,
  type ScreenSharePipelineConfig,
  type MediaPipelineStatus,
} from './MediaPipeline';

// Screen share optimization
export {
  ScreenShareOptimizer,
  screenShareOptimizer,
  type ScreenContentType,
  type ScreenActivityState,
  type RegionOfInterest,
  type ScreenShareQualityFeedback,
} from './ScreenShareOptimizer';

// UI render optimization
export {
  createStableCallback,
  createSelector,
  createSelectorMulti,
  RAFScheduler,
  rafScheduler,
  RenderProfiler,
  renderProfiler,
  DebouncedValue,
  shouldVirtualize,
  VIRTUALIZATION_CONFIGS,
  type RenderProfile,
  type VirtualizationConfig,
} from './UIRenderEngine';

// Backend configuration
export {
  generateBackendConfig,
  generateSQLitePragmas,
  generateBackendConfigJSON,
  generateBackendEnvVars,
  type SQLiteConfig,
  type APIConfig,
  type SocketIOConfig,
  type QueryBatchConfig,
  type BackgroundTaskConfig,
  type FileTransferConfig,
  type BackendConfig,
} from './BackendOptimizer';

// Group call resource management
export {
  GroupCallResourceManager,
  groupCallResourceManager,
  type ParticipantResourceBudget,
  type GroupResourceAllocation,
  type ICECandidateBatch,
} from './GroupCallResourceManager';
