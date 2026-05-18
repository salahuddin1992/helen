/**
 * RenderOptimizer.ts — DOM/React rendering cost reduction engine.
 *
 * Applies and removes visual optimizations based on the active RenderBudget
 * from HardwareProfiles. Works by:
 *
 *   1. CSS Class Injection: Adds/removes utility classes on <html> that
 *      disable animations, blur, shadows via CSS overrides (no component changes).
 *   2. CSS Custom Properties: Sets --render-* variables that components can
 *      read for conditional rendering.
 *   3. DOM Complexity Tracking: Monitors total DOM node count and warns if
 *      it exceeds thresholds.
 *   4. Virtualization Hints: Exposes shouldVirtualize(listSize) for components
 *      to decide whether to use windowed rendering.
 *
 * CSS override strategy (injected <style> tag):
 *   .render-no-animations * { animation: none !important; transition: none !important; }
 *   .render-no-blur * { backdrop-filter: none !important; }
 *   .render-no-shadows * { box-shadow: none !important; }
 *   .render-reduced-motion * { transition-duration: 0.01ms !important; }
 *
 * This approach is zero-config for existing components — they don't need
 * to import or check anything. The CSS classes cascade globally.
 */

import type { RenderBudget } from './HardwareProfiles';

// ── Types ───────────────────────────────────────────────────

export interface RenderMetrics {
  /** Total DOM nodes in document */
  domNodeCount: number;
  /** Whether animations are currently enabled */
  animationsEnabled: boolean;
  /** Whether blur effects are currently enabled */
  blurEnabled: boolean;
  /** Whether shadows are currently enabled */
  shadowsEnabled: boolean;
  /** Current target FPS */
  targetFps: number;
  /** Chat virtualization threshold */
  virtualizeThreshold: number;
  /** Active animated element count (approximate) */
  animatedElementCount: number;
}

export interface DOMComplexityWarning {
  nodeCount: number;
  threshold: number;
  message: string;
  timestamp: number;
}

type ComplexityCallback = (warning: DOMComplexityWarning) => void;

// ── Constants ───────────────────────────────────────────────

const STYLE_TAG_ID = 'commclient-render-optimizer';
const DOM_CHECK_INTERVAL_MS = 10_000;
const DOM_WARNING_THRESHOLD = 5_000;
const DOM_CRITICAL_THRESHOLD = 15_000;
const ANIMATED_ELEMENT_SELECTOR = '[class*="animate-"], [class*="transition-"]';

// ── CSS Override Templates ──────────────────────────────────

const CSS_OVERRIDES = `
/* RenderOptimizer — injected overrides */
html.render-no-animations *,
html.render-no-animations *::before,
html.render-no-animations *::after {
  animation-duration: 0.01ms !important;
  animation-iteration-count: 1 !important;
  animation-delay: 0ms !important;
  transition-duration: 0.01ms !important;
  transition-delay: 0ms !important;
}

html.render-no-blur *,
html.render-no-blur *::before,
html.render-no-blur *::after {
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
}

html.render-no-shadows *,
html.render-no-shadows *::before,
html.render-no-shadows *::after {
  box-shadow: none !important;
  text-shadow: none !important;
}

html.render-no-smooth-scroll {
  scroll-behavior: auto !important;
}
html.render-no-smooth-scroll * {
  scroll-behavior: auto !important;
}

html.render-no-avatar-images .avatar-image {
  display: none !important;
}
html.render-no-avatar-images .avatar-initials {
  display: flex !important;
}

html.render-no-typing .typing-indicator {
  display: none !important;
}

/* Performance FPS target via will-change hint removal */
html.render-low-fps * {
  will-change: auto !important;
}
`.trim();

// ── RenderOptimizer ─────────────────────────────────────────

export class RenderOptimizer {
  private _budget: RenderBudget | null = null;
  private _styleElement: HTMLStyleElement | null = null;
  private _domCheckTimer: ReturnType<typeof setInterval> | null = null;
  private _complexityListeners: ComplexityCallback[] = [];
  private _destroyed = false;
  private _appliedClasses: Set<string> = new Set();

  // ── Lifecycle ─────────────────────────────────────────────

  start(): void {
    if (this._destroyed) return;
    this._injectStyleTag();
    this._domCheckTimer = setInterval(() => this._checkDOMComplexity(), DOM_CHECK_INTERVAL_MS);
  }

  stop(): void {
    this._removeAllClasses();
    this._removeStyleTag();
    if (this._domCheckTimer) {
      clearInterval(this._domCheckTimer);
      this._domCheckTimer = null;
    }
  }

  destroy(): void {
    this._destroyed = true;
    this.stop();
    this._complexityListeners = [];
  }

  // ── Apply Budget ──────────────────────────────────────────

  /**
   * Apply a RenderBudget. Adds/removes CSS classes on <html>
   * and sets CSS custom properties.
   */
  applyBudget(budget: RenderBudget): void {
    this._budget = budget;
    if (this._destroyed) return;

    const html = document.documentElement;

    // ── Animations ──────────────────────────────────────
    this._toggleClass(html, 'render-no-animations', !budget.enableAnimations);

    // ── Backdrop blur ───────────────────────────────────
    this._toggleClass(html, 'render-no-blur', !budget.enableBackdropBlur);

    // ── Shadows ─────────────────────────────────────────
    this._toggleClass(html, 'render-no-shadows', !budget.enableShadows);

    // ── Smooth scroll ───────────────────────────────────
    this._toggleClass(html, 'render-no-smooth-scroll', !budget.enableSmoothScroll);

    // ── Avatar images ───────────────────────────────────
    this._toggleClass(html, 'render-no-avatar-images', !budget.enableAvatarImages);

    // ── Typing animation ────────────────────────────────
    this._toggleClass(html, 'render-no-typing', !budget.enableTypingAnimation);

    // ── Low FPS mode ────────────────────────────────────
    this._toggleClass(html, 'render-low-fps', budget.targetFps < 60);

    // ── CSS Custom Properties ───────────────────────────
    html.style.setProperty('--render-target-fps', String(budget.targetFps));
    html.style.setProperty('--render-max-animated', String(budget.maxAnimatedElements));
    html.style.setProperty('--render-virtualize-at', String(budget.chatVirtualizeThreshold));
    html.style.setProperty('--render-search-debounce', `${budget.searchDebounceMs}ms`);
  }

  /**
   * Remove all render optimizations (restore full quality).
   */
  removeAll(): void {
    this._removeAllClasses();
    const html = document.documentElement;
    html.style.removeProperty('--render-target-fps');
    html.style.removeProperty('--render-max-animated');
    html.style.removeProperty('--render-virtualize-at');
    html.style.removeProperty('--render-search-debounce');
  }

  // ── Query Methods ─────────────────────────────────────────

  /**
   * Whether a list of given size should use virtualized rendering.
   */
  shouldVirtualize(listSize: number): boolean {
    if (!this._budget) return listSize > 100;
    return listSize > this._budget.chatVirtualizeThreshold;
  }

  /**
   * Get the current search input debounce interval.
   */
  getSearchDebounceMs(): number {
    return this._budget?.searchDebounceMs ?? 300;
  }

  /**
   * Get current render metrics.
   */
  getMetrics(): RenderMetrics {
    let domNodeCount = 0;
    let animatedElementCount = 0;
    try {
      domNodeCount = document.querySelectorAll('*').length;
      animatedElementCount = document.querySelectorAll(ANIMATED_ELEMENT_SELECTOR).length;
    } catch {}

    return {
      domNodeCount,
      animationsEnabled: this._budget?.enableAnimations ?? true,
      blurEnabled: this._budget?.enableBackdropBlur ?? true,
      shadowsEnabled: this._budget?.enableShadows ?? true,
      targetFps: this._budget?.targetFps ?? 60,
      virtualizeThreshold: this._budget?.chatVirtualizeThreshold ?? 100,
      animatedElementCount,
    };
  }

  /**
   * Check if animations are currently enabled.
   */
  areAnimationsEnabled(): boolean {
    return this._budget?.enableAnimations ?? true;
  }

  // ── DOM Complexity Monitoring ─────────────────────────────

  onComplexityWarning(cb: ComplexityCallback): () => void {
    this._complexityListeners.push(cb);
    return () => {
      this._complexityListeners = this._complexityListeners.filter(l => l !== cb);
    };
  }

  private _checkDOMComplexity(): void {
    if (this._destroyed) return;

    let nodeCount = 0;
    try {
      nodeCount = document.querySelectorAll('*').length;
    } catch { return; }

    if (nodeCount > DOM_CRITICAL_THRESHOLD) {
      this._emitComplexity({
        nodeCount,
        threshold: DOM_CRITICAL_THRESHOLD,
        message: `Critical DOM complexity: ${nodeCount} nodes (limit: ${DOM_CRITICAL_THRESHOLD})`,
        timestamp: Date.now(),
      });
    } else if (nodeCount > DOM_WARNING_THRESHOLD) {
      this._emitComplexity({
        nodeCount,
        threshold: DOM_WARNING_THRESHOLD,
        message: `High DOM complexity: ${nodeCount} nodes (recommended: <${DOM_WARNING_THRESHOLD})`,
        timestamp: Date.now(),
      });
    }
  }

  private _emitComplexity(warning: DOMComplexityWarning): void {
    for (const cb of this._complexityListeners) {
      try { cb(warning); } catch {}
    }
  }

  // ── Internal: Style Tag Management ────────────────────────

  private _injectStyleTag(): void {
    if (document.getElementById(STYLE_TAG_ID)) return;

    const style = document.createElement('style');
    style.id = STYLE_TAG_ID;
    style.textContent = CSS_OVERRIDES;
    document.head.appendChild(style);
    this._styleElement = style;
  }

  private _removeStyleTag(): void {
    if (this._styleElement) {
      this._styleElement.remove();
      this._styleElement = null;
    }
  }

  // ── Internal: Class Management ────────────────────────────

  private _toggleClass(el: HTMLElement, className: string, add: boolean): void {
    if (add) {
      el.classList.add(className);
      this._appliedClasses.add(className);
    } else {
      el.classList.remove(className);
      this._appliedClasses.delete(className);
    }
  }

  private _removeAllClasses(): void {
    const html = document.documentElement;
    for (const cls of this._appliedClasses) {
      html.classList.remove(cls);
    }
    this._appliedClasses.clear();
  }
}
