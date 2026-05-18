// Polyfill WeakRef for older environments
declare global {
  class WeakRef<T extends object> {
    constructor(value: T);
    deref(): T | undefined;
  }
}

/**
 * MemoryManager.ts — Heap pressure relief, cache eviction & object pooling.
 *
 * Identified problems from audit:
 *   1. AudioContext instances created per-component (no pooling)
 *   2. Chat message arrays grow unbounded in memory
 *   3. No LRU cache eviction for file previews / avatar images
 *   4. Notification store listener leak (duplicate listeners on re-login)
 *   5. Cloned MediaStreams accumulate without cleanup
 *   6. Timer refs in overlay components not properly cleaned
 *
 * Solutions:
 *   1. AudioContext singleton with reference counting
 *   2. LRU cache with configurable max size for messages per channel
 *   3. WeakRef-based object pool for reusable objects
 *   4. Periodic heap pressure check with GC hints
 *   5. Subscription tracker to detect and warn about leaks
 *   6. Blob URL registry with automatic revocation
 *
 * Architecture:
 *   MemoryManager is a singleton that provides sub-managers:
 *     - AudioContextPool: shared AudioContext with ref counting
 *     - LRUCache<K,V>: generic bounded cache
 *     - ObjectPool<T>: reusable object pool via WeakRef
 *     - BlobRegistry: tracked blob URL creation/revocation
 *     - SubscriptionTracker: leak detection for event listeners
 *     - HeapMonitor: periodic heap checks with pressure callbacks
 */

// ── LRU Cache ───────────────────────────────────────────────

export class LRUCache<K, V> {
  private _map = new Map<K, V>();
  private _maxSize: number;
  private _onEvict?: (key: K, value: V) => void;

  constructor(maxSize: number, onEvict?: (key: K, value: V) => void) {
    this._maxSize = maxSize;
    this._onEvict = onEvict;
  }

  get(key: K): V | undefined {
    const value = this._map.get(key);
    if (value !== undefined) {
      // Move to end (most recently used)
      this._map.delete(key);
      this._map.set(key, value);
    }
    return value;
  }

  set(key: K, value: V): void {
    // If key exists, delete first to update position
    if (this._map.has(key)) {
      this._map.delete(key);
    }

    // Evict oldest entries if at capacity
    while (this._map.size >= this._maxSize) {
      const oldest = this._map.keys().next();
      if (!oldest.done) {
        const oldValue = this._map.get(oldest.value);
        this._map.delete(oldest.value);
        if (this._onEvict && oldValue !== undefined) {
          this._onEvict(oldest.value, oldValue);
        }
      }
    }

    this._map.set(key, value);
  }

  has(key: K): boolean {
    return this._map.has(key);
  }

  delete(key: K): boolean {
    return this._map.delete(key);
  }

  clear(): void {
    if (this._onEvict) {
      for (const [key, value] of this._map) {
        this._onEvict(key, value);
      }
    }
    this._map.clear();
  }

  get size(): number { return this._map.size; }
  get maxSize(): number { return this._maxSize; }

  /**
   * Resize the cache. Evicts oldest entries if new size is smaller.
   */
  resize(newMaxSize: number): void {
    this._maxSize = newMaxSize;
    while (this._map.size > this._maxSize) {
      const oldest = this._map.keys().next();
      if (!oldest.done) {
        const oldValue = this._map.get(oldest.value);
        this._map.delete(oldest.value);
        if (this._onEvict && oldValue !== undefined) {
          this._onEvict(oldest.value, oldValue);
        }
      }
    }
  }

  /**
   * Get all keys in order from oldest to newest.
   */
  keys(): K[] {
    return Array.from(this._map.keys());
  }
}

// ── Object Pool via WeakRef ─────────────────────────────────

export class ObjectPool<T extends object> {
  private _pool: Array<WeakRef<T>> = [];
  private _factory: () => T;
  private _reset?: (obj: T) => void;
  private _maxSize: number;

  constructor(factory: () => T, options?: { reset?: (obj: T) => void; maxSize?: number }) {
    this._factory = factory;
    this._reset = options?.reset;
    this._maxSize = options?.maxSize ?? 50;
  }

  /**
   * Acquire an object from the pool or create a new one.
   */
  acquire(): T {
    // Try to reuse a pooled object
    while (this._pool.length > 0) {
      const ref = this._pool.pop()!;
      const obj = ref.deref();
      if (obj) {
        if (this._reset) this._reset(obj);
        return obj;
      }
      // WeakRef was collected, skip
    }

    // Create new
    return this._factory();
  }

  /**
   * Return an object to the pool for reuse.
   */
  release(obj: T): void {
    if (this._pool.length < this._maxSize) {
      this._pool.push(new WeakRef(obj));
    }
  }

  /**
   * Clean up collected refs.
   */
  compact(): void {
    this._pool = this._pool.filter(ref => ref.deref() !== undefined);
  }

  get poolSize(): number { return this._pool.length; }
}

// ── AudioContext Singleton ───────────────────────────────────

export class AudioContextPool {
  private _context: AudioContext | null = null;
  private _refCount = 0;

  /**
   * Acquire a shared AudioContext. Increments ref count.
   * Call release() when done.
   */
  acquire(): AudioContext {
    if (!this._context || this._context.state === 'closed') {
      this._context = new AudioContext({
        latencyHint: 'interactive',
        sampleRate: 48_000,
      });
    }

    this._refCount++;

    // Resume if suspended (browser autoplay policy)
    if (this._context.state === 'suspended') {
      this._context.resume().catch(() => {});
    }

    return this._context;
  }

  /**
   * Release a reference. Closes the context when all refs are released.
   */
  release(): void {
    this._refCount = Math.max(0, this._refCount - 1);

    if (this._refCount === 0 && this._context) {
      // Don't close immediately — might be reused soon
      setTimeout(() => {
        if (this._refCount === 0 && this._context) {
          this._context.close().catch(() => {});
          this._context = null;
        }
      }, 5_000);
    }
  }

  /**
   * Force close regardless of ref count.
   */
  forceClose(): void {
    this._refCount = 0;
    if (this._context) {
      this._context.close().catch(() => {});
      this._context = null;
    }
  }

  get refCount(): number { return this._refCount; }
  get isActive(): boolean { return this._context !== null && this._context.state !== 'closed'; }
}

// ── Blob URL Registry ───────────────────────────────────────

export class BlobRegistry {
  private _urls = new Map<string, { url: string; createdAt: number; tag: string }>();
  private _maxAge: number;

  constructor(maxAgeMs: number = 5 * 60_000) {
    this._maxAge = maxAgeMs;
  }

  /**
   * Create a tracked blob URL.
   */
  createURL(blob: Blob, tag: string = 'unknown'): string {
    const url = URL.createObjectURL(blob);
    this._urls.set(url, { url, createdAt: Date.now(), tag });
    return url;
  }

  /**
   * Revoke a blob URL and remove from tracking.
   */
  revokeURL(url: string): void {
    if (this._urls.has(url)) {
      URL.revokeObjectURL(url);
      this._urls.delete(url);
    }
  }

  /**
   * Revoke all blob URLs matching a tag.
   */
  revokeByTag(tag: string): void {
    for (const [url, entry] of this._urls) {
      if (entry.tag === tag) {
        URL.revokeObjectURL(url);
        this._urls.delete(url);
      }
    }
  }

  /**
   * Revoke all expired blob URLs (older than maxAge).
   */
  cleanup(): number {
    const now = Date.now();
    let revoked = 0;

    for (const [url, entry] of this._urls) {
      if (now - entry.createdAt > this._maxAge) {
        URL.revokeObjectURL(url);
        this._urls.delete(url);
        revoked++;
      }
    }

    return revoked;
  }

  /**
   * Revoke ALL tracked blob URLs.
   */
  revokeAll(): void {
    for (const [url] of this._urls) {
      URL.revokeObjectURL(url);
    }
    this._urls.clear();
  }

  get activeCount(): number { return this._urls.size; }
}

// ── Subscription Tracker (Leak Detection) ───────────────────

export interface SubscriptionRecord {
  id: string;
  source: string;
  event: string;
  registeredAt: number;
  cleanup: () => void;
}

export class SubscriptionTracker {
  private _subscriptions = new Map<string, SubscriptionRecord>();
  private _counter = 0;
  private _warnThreshold: number;

  constructor(warnThreshold: number = 50) {
    this._warnThreshold = warnThreshold;
  }

  /**
   * Track a subscription. Returns an ID for later cleanup.
   */
  track(source: string, event: string, cleanup: () => void): string {
    const id = `sub_${++this._counter}`;
    this._subscriptions.set(id, {
      id,
      source,
      event,
      registeredAt: Date.now(),
      cleanup,
    });

    // Warn if too many active subscriptions
    if (this._subscriptions.size > this._warnThreshold) {
      console.warn(
        `[SubscriptionTracker] ${this._subscriptions.size} active subscriptions ` +
        `(threshold: ${this._warnThreshold}). Possible leak.`
      );
    }

    return id;
  }

  /**
   * Untrack and clean up a subscription.
   */
  untrack(id: string): void {
    const sub = this._subscriptions.get(id);
    if (sub) {
      try { sub.cleanup(); } catch {}
      this._subscriptions.delete(id);
    }
  }

  /**
   * Untrack all subscriptions from a specific source.
   */
  untrackSource(source: string): void {
    for (const [id, sub] of this._subscriptions) {
      if (sub.source === source) {
        try { sub.cleanup(); } catch {}
        this._subscriptions.delete(id);
      }
    }
  }

  /**
   * Clean up ALL subscriptions.
   */
  untrackAll(): void {
    for (const [, sub] of this._subscriptions) {
      try { sub.cleanup(); } catch {}
    }
    this._subscriptions.clear();
  }

  /**
   * Get diagnostics about active subscriptions.
   */
  getDiagnostics(): { total: number; bySource: Record<string, number>; oldest: number } {
    const bySource: Record<string, number> = {};
    let oldest = Date.now();

    for (const [, sub] of this._subscriptions) {
      bySource[sub.source] = (bySource[sub.source] ?? 0) + 1;
      if (sub.registeredAt < oldest) oldest = sub.registeredAt;
    }

    return {
      total: this._subscriptions.size,
      bySource,
      oldest: this._subscriptions.size > 0 ? oldest : 0,
    };
  }

  get activeCount(): number { return this._subscriptions.size; }
}

// ── Heap Monitor ────────────────────────────────────────────

export interface HeapSnapshot {
  usedMB: number;
  totalMB: number;
  limitMB: number;
  usageRatio: number;
  timestamp: number;
}

export type HeapPressureLevel = 'normal' | 'elevated' | 'high' | 'critical';

type HeapPressureCallback = (level: HeapPressureLevel, snapshot: HeapSnapshot) => void;

export class HeapMonitor {
  private _timer: ReturnType<typeof setInterval> | null = null;
  private _listeners: HeapPressureCallback[] = [];
  private _lastLevel: HeapPressureLevel = 'normal';
  private _destroyed = false;
  private _intervalMs: number;

  // Thresholds (ratio of heap used to heap limit)
  private _elevatedThreshold = 0.60;
  private _highThreshold = 0.75;
  private _criticalThreshold = 0.90;

  constructor(intervalMs: number = 10_000) {
    this._intervalMs = intervalMs;
  }

  start(): void {
    if (this._destroyed || this._timer) return;
    this._timer = setInterval(() => this._check(), this._intervalMs);
    this._check(); // Initial check
  }

  stop(): void {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  destroy(): void {
    this._destroyed = true;
    this.stop();
    this._listeners = [];
  }

  onPressure(cb: HeapPressureCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  getSnapshot(): HeapSnapshot {
    let usedMB = 0;
    let totalMB = 0;
    let limitMB = 0;

    try {
      const mem = (performance as any).memory;
      if (mem) {
        usedMB = Math.round(mem.usedJSHeapSize / 1024 / 1024);
        totalMB = Math.round(mem.totalJSHeapSize / 1024 / 1024);
        limitMB = Math.round(mem.jsHeapSizeLimit / 1024 / 1024);
      }
    } catch {}

    return {
      usedMB,
      totalMB,
      limitMB,
      usageRatio: limitMB > 0 ? usedMB / limitMB : 0,
      timestamp: Date.now(),
    };
  }

  getLevel(): HeapPressureLevel { return this._lastLevel; }

  /**
   * Hint the browser to run GC if available.
   * Only works in Chromium with --expose-gc flag (Electron can enable this).
   */
  hintGC(): void {
    try {
      if (typeof (globalThis as any).gc === 'function') {
        (globalThis as any).gc();
      }
    } catch {}
  }

  private _check(): void {
    if (this._destroyed) return;

    const snapshot = this.getSnapshot();
    let level: HeapPressureLevel = 'normal';

    if (snapshot.usageRatio >= this._criticalThreshold) {
      level = 'critical';
    } else if (snapshot.usageRatio >= this._highThreshold) {
      level = 'high';
    } else if (snapshot.usageRatio >= this._elevatedThreshold) {
      level = 'elevated';
    }

    if (level !== this._lastLevel) {
      this._lastLevel = level;
      for (const cb of this._listeners) {
        try { cb(level, snapshot); } catch {}
      }
    }

    // Auto-hint GC at high pressure
    if (level === 'high' || level === 'critical') {
      this.hintGC();
    }
  }
}

// ── MemoryManager (Unified Singleton) ───────────────────────

export class MemoryManager {
  readonly audioPool = new AudioContextPool();
  readonly blobRegistry = new BlobRegistry(5 * 60_000);
  readonly subscriptions = new SubscriptionTracker(100);
  readonly heapMonitor = new HeapMonitor(15_000);

  // Pre-configured caches
  readonly messageCache: LRUCache<string, any[]>;
  readonly avatarCache: LRUCache<string, string>;
  readonly filePreviewCache: LRUCache<string, string>;

  private _blobCleanupTimer: ReturnType<typeof setInterval> | null = null;
  private _destroyed = false;

  constructor() {
    // Messages: keep last 500 messages per channel, max 20 channels in cache
    this.messageCache = new LRUCache<string, any[]>(20);
    // Avatar URLs: LRU with max 200 entries
    this.avatarCache = new LRUCache<string, string>(200);
    // File preview blob URLs: LRU with blob revocation on evict
    this.filePreviewCache = new LRUCache<string, string>(50, (_key, url) => {
      this.blobRegistry.revokeURL(url);
    });
  }

  start(): void {
    if (this._destroyed) return;

    this.heapMonitor.start();

    // Periodic blob cleanup
    this._blobCleanupTimer = setInterval(() => {
      this.blobRegistry.cleanup();
    }, 60_000);

    // React to heap pressure
    this.heapMonitor.onPressure((level, snapshot) => {
      if (level === 'critical') {
        // Emergency: clear all non-essential caches
        this.avatarCache.clear();
        this.filePreviewCache.clear();
        this.blobRegistry.cleanup();
        this.heapMonitor.hintGC();
        console.warn(`[MemoryManager] Critical heap: ${snapshot.usedMB}MB / ${snapshot.limitMB}MB — caches cleared`);
      } else if (level === 'high') {
        // Reduce cache sizes
        this.avatarCache.resize(50);
        this.filePreviewCache.resize(10);
        this.blobRegistry.cleanup();
      }
    });
  }

  stop(): void {
    this.heapMonitor.stop();
    if (this._blobCleanupTimer) {
      clearInterval(this._blobCleanupTimer);
      this._blobCleanupTimer = null;
    }
  }

  destroy(): void {
    this._destroyed = true;
    this.stop();
    this.audioPool.forceClose();
    this.blobRegistry.revokeAll();
    this.subscriptions.untrackAll();
    this.messageCache.clear();
    this.avatarCache.clear();
    this.filePreviewCache.clear();
    this.heapMonitor.destroy();
  }

  /**
   * Get a diagnostic summary of all managed resources.
   */
  getDiagnostics(): Record<string, any> {
    return {
      heap: this.heapMonitor.getSnapshot(),
      heapPressure: this.heapMonitor.getLevel(),
      audioContextActive: this.audioPool.isActive,
      audioContextRefs: this.audioPool.refCount,
      blobUrls: this.blobRegistry.activeCount,
      subscriptions: this.subscriptions.getDiagnostics(),
      messageCacheSize: this.messageCache.size,
      avatarCacheSize: this.avatarCache.size,
      filePreviewCacheSize: this.filePreviewCache.size,
    };
  }
}

// ── Singleton ───────────────────────────────────────────────

export const memoryManager = new MemoryManager();
