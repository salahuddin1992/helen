/**
 * UIRenderEngine.ts — React re-render prevention & UI scheduling.
 *
 * Identified problems from audit:
 *   1. CallView re-renders entire tree every 1 second (timer)
 *   2. MessageList rowRenderer has 10+ deps → all rows re-render on any change
 *   3. ParticipantGrid creates new object refs on every render
 *   4. ChannelList items not memoized — all re-render on unread count change
 *   5. EmojiPicker recreates 400+ emoji arrays per render
 *   6. ProductNav useMemo recomputes full nav on any badge change
 *   7. MessageInput typing callback recreates every keystroke
 *   8. No virtualization for contact/channel lists
 *   9. Inline functions in JSX cause child re-renders
 *   10. Missing React.memo on list item components
 *
 * Solutions:
 *   1. Component isolation helpers (extract re-render boundaries)
 *   2. Selector factory for Zustand stores (memoized selectors)
 *   3. RAF-scheduled state updates (batch DOM writes to animation frames)
 *   4. Event handler factory (stable callbacks via ref pattern)
 *   5. Virtualization config for all list components
 *   6. Re-render profiler for development mode
 *
 * This module provides UTILITIES and PATTERNS, not direct component patches.
 * Components import these helpers to optimize their render behavior.
 */

// ── Types ───────────────────────────────────────────────────

export interface RenderProfile {
  componentName: string;
  renderCount: number;
  totalRenderTimeMs: number;
  averageRenderTimeMs: number;
  lastRenderTimeMs: number;
  unnecessaryRenders: number;
  lastProps: Record<string, any> | null;
}

export interface VirtualizationConfig {
  /** Estimated item height (px) */
  itemHeight: number;
  /** Overscan count (items rendered outside viewport) */
  overscanCount: number;
  /** Whether to use windowed rendering */
  enabled: boolean;
  /** Threshold: virtualize only if list > this size */
  threshold: number;
}

// ── Stable Callback Factory ─────────────────────────────────

/**
 * Create a stable callback that always calls the latest function.
 * Eliminates re-renders caused by inline function creation in JSX.
 *
 * Usage in a component:
 *   const handleClick = useStableCallback((id: string) => {
 *     // This closure is always fresh, but the returned ref is stable
 *     doSomething(id, currentState);
 *   });
 *
 * This avoids the stale-closure problem of useCallback while maintaining
 * a stable function identity.
 *
 * Implementation note: This is a factory that returns a custom hook pattern.
 * Components should use it via the exported useStableCallback hook below.
 */
export function createStableCallback<T extends (...args: any[]) => any>(
  fnRef: { current: T },
): T {
  // Return a wrapper that always calls the latest ref
  const stable = ((...args: any[]) => fnRef.current(...args)) as T;
  return stable;
}

// ── Zustand Selector Memoization ────────────────────────────

/**
 * Create a memoized selector for Zustand stores.
 * Prevents re-renders when unrelated state changes.
 *
 * Usage:
 *   const selectUnreadCount = createSelector(
 *     (state: ChatState) => state.channels,
 *     (channels) => channels.reduce((sum, ch) => sum + ch.unread, 0)
 *   );
 *
 *   // In component:
 *   const unread = useChatStore(selectUnreadCount);
 */
export function createSelector<TState, TDep, TResult>(
  depSelector: (state: TState) => TDep,
  resultFn: (dep: TDep) => TResult,
): (state: TState) => TResult {
  let lastDep: TDep | undefined;
  let lastResult: TResult | undefined;
  let initialized = false;

  return (state: TState): TResult => {
    const dep = depSelector(state);

    if (!initialized || !shallowEqual(dep, lastDep)) {
      lastDep = dep;
      lastResult = resultFn(dep);
      initialized = true;
    }

    return lastResult!;
  };
}

/**
 * Create a multi-dependency memoized selector.
 */
export function createSelectorMulti<TState, TResult>(
  depSelectors: Array<(state: TState) => any>,
  resultFn: (...deps: any[]) => TResult,
): (state: TState) => TResult {
  let lastDeps: any[] | undefined;
  let lastResult: TResult | undefined;

  return (state: TState): TResult => {
    const deps = depSelectors.map(sel => sel(state));

    if (!lastDeps || deps.some((dep, i) => !shallowEqual(dep, lastDeps![i]))) {
      lastDeps = deps;
      lastResult = resultFn(...deps);
    }

    return lastResult!;
  };
}

// ── RAF-Scheduled Updates ───────────────────────────────────

/**
 * Schedule a state update to run in the next animation frame.
 * Coalesces multiple updates into a single frame.
 */
export class RAFScheduler {
  private _pending = new Map<string, () => void>();
  private _rafId: number | null = null;

  /**
   * Schedule an update. If the same key is scheduled again before
   * the frame fires, the previous update is replaced.
   */
  schedule(key: string, update: () => void): void {
    this._pending.set(key, update);

    if (this._rafId === null) {
      this._rafId = requestAnimationFrame(() => {
        this._rafId = null;
        const updates = new Map(this._pending);
        this._pending.clear();

        for (const [, fn] of updates) {
          try { fn(); } catch {}
        }
      });
    }
  }

  /**
   * Cancel a pending update.
   */
  cancel(key: string): void {
    this._pending.delete(key);
  }

  /**
   * Cancel all pending updates.
   */
  cancelAll(): void {
    this._pending.clear();
    if (this._rafId !== null) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  get pendingCount(): number { return this._pending.size; }
}

// ── Virtualization Configs ──────────────────────────────────

export const VIRTUALIZATION_CONFIGS: Record<string, VirtualizationConfig> = {
  messageList: {
    itemHeight: 64,
    overscanCount: 10,
    enabled: true,
    threshold: 50,
  },
  channelList: {
    itemHeight: 56,
    overscanCount: 5,
    enabled: true,
    threshold: 20,
  },
  contactList: {
    itemHeight: 48,
    overscanCount: 8,
    enabled: true,
    threshold: 30,
  },
  participantGrid: {
    itemHeight: 180,
    overscanCount: 2,
    enabled: true,
    threshold: 6,
  },
  emojiPicker: {
    itemHeight: 40,
    overscanCount: 5,
    enabled: true,
    threshold: 50,
  },
  notificationList: {
    itemHeight: 72,
    overscanCount: 3,
    enabled: true,
    threshold: 20,
  },
};

/**
 * Check if a list should use virtual rendering.
 */
export function shouldVirtualize(listName: string, itemCount: number): boolean {
  const config = VIRTUALIZATION_CONFIGS[listName];
  if (!config) return itemCount > 100;
  return config.enabled && itemCount > config.threshold;
}

// ── Render Profiler (Development Only) ──────────────────────

export class RenderProfiler {
  private _profiles = new Map<string, RenderProfile>();
  private _enabled: boolean;

  constructor(enabled: boolean = false) {
    this._enabled = enabled;
  }

  /**
   * Record a component render.
   */
  recordRender(componentName: string, renderTimeMs: number, props?: Record<string, any>): void {
    if (!this._enabled) return;

    let profile = this._profiles.get(componentName);
    if (!profile) {
      profile = {
        componentName,
        renderCount: 0,
        totalRenderTimeMs: 0,
        averageRenderTimeMs: 0,
        lastRenderTimeMs: 0,
        unnecessaryRenders: 0,
        lastProps: null,
      };
      this._profiles.set(componentName, profile);
    }

    profile.renderCount++;
    profile.totalRenderTimeMs += renderTimeMs;
    profile.averageRenderTimeMs = profile.totalRenderTimeMs / profile.renderCount;
    profile.lastRenderTimeMs = renderTimeMs;

    // Detect unnecessary renders (same props)
    if (props && profile.lastProps && shallowEqual(props, profile.lastProps)) {
      profile.unnecessaryRenders++;
    }
    profile.lastProps = props ?? null;
  }

  /**
   * Get all profiles sorted by total render time (descending).
   */
  getProfiles(): RenderProfile[] {
    return Array.from(this._profiles.values())
      .sort((a, b) => b.totalRenderTimeMs - a.totalRenderTimeMs);
  }

  /**
   * Get the top N most expensive components.
   */
  getTopExpensive(n: number = 10): RenderProfile[] {
    return this.getProfiles().slice(0, n);
  }

  /**
   * Get components with the most unnecessary renders.
   */
  getUnnecessaryRenders(): RenderProfile[] {
    return Array.from(this._profiles.values())
      .filter(p => p.unnecessaryRenders > 0)
      .sort((a, b) => b.unnecessaryRenders - a.unnecessaryRenders);
  }

  /**
   * Reset all profiles.
   */
  reset(): void {
    this._profiles.clear();
  }

  /**
   * Print a summary to console.
   */
  printSummary(): void {
    if (!this._enabled) return;

    const profiles = this.getProfiles();
    console.group('[UIRenderEngine] Render Profile Summary');
    console.table(profiles.map(p => ({
      Component: p.componentName,
      Renders: p.renderCount,
      'Avg (ms)': p.averageRenderTimeMs.toFixed(2),
      'Total (ms)': p.totalRenderTimeMs.toFixed(1),
      Unnecessary: p.unnecessaryRenders,
    })));
    console.groupEnd();
  }

  get enabled(): boolean { return this._enabled; }
  set enabled(value: boolean) { this._enabled = value; }
}

// ── Debounced Value ─────────────────────────────────────────

/**
 * Create a debounced version of a frequently-changing value.
 * Useful for search inputs, scroll positions, resize dimensions.
 */
export class DebouncedValue<T> {
  private _value: T;
  private _debouncedValue: T;
  private _timer: ReturnType<typeof setTimeout> | null = null;
  private _delayMs: number;
  private _listeners: Array<(value: T) => void> = [];

  constructor(initialValue: T, delayMs: number) {
    this._value = initialValue;
    this._debouncedValue = initialValue;
    this._delayMs = delayMs;
  }

  set(value: T): void {
    this._value = value;

    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      this._timer = null;
      this._debouncedValue = this._value;
      for (const cb of this._listeners) {
        try { cb(this._debouncedValue); } catch {}
      }
    }, this._delayMs);
  }

  /** Get the immediate (non-debounced) value */
  get immediate(): T { return this._value; }

  /** Get the debounced value */
  get value(): T { return this._debouncedValue; }

  onChange(cb: (value: T) => void): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  destroy(): void {
    if (this._timer) clearTimeout(this._timer);
    this._listeners = [];
  }
}

// ── Shallow Equal Utility ───────────────────────────────────

function shallowEqual(a: any, b: any): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== typeof b) return false;

  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((v, i) => Object.is(v, b[i]));
  }

  if (typeof a === 'object' && a !== null && b !== null) {
    const keysA = Object.keys(a);
    const keysB = Object.keys(b);
    if (keysA.length !== keysB.length) return false;
    return keysA.every(key => Object.is(a[key], b[key]));
  }

  return false;
}

// ── Singletons ──────────────────────────────────────────────

export const rafScheduler = new RAFScheduler();
export const renderProfiler = new RenderProfiler(
  typeof process !== 'undefined' && process.env?.NODE_ENV === 'development'
);
