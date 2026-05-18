/**
 * NetworkAdaptiveVideo — auto-pauses local video when the call's
 * QualityController reports sustained "poor" network, and resumes
 * once quality recovers. The user gets a banner explaining what
 * happened plus a "keep video on anyway" override.
 *
 * Why a separate file
 * -------------------
 * The QualityController already exists; CallControls already has a
 * manual video toggle. What was missing was the *closed loop* —
 * something that watches the quality stream and *acts*. Putting
 * that policy here keeps it isolated, testable, and easy to
 * disable wholesale (single ``stop()`` call from the lifecycle).
 *
 * Policy
 * ------
 *   * Poor for ``sustainedMs`` (default 4s)  → auto-pause local video.
 *   * Excellent/good for ``recoverMs`` (default 6s) → auto-resume,
 *     but only if we paused. If the user toggled manually we never
 *     un-do that.
 *   * Manual override: when the user toggles video while we have it
 *     auto-paused, we treat that as "the user wants control" and
 *     stop adapting until a new call begins.
 *
 * Wiring
 * ------
 *   const adapter = new NetworkAdaptiveVideo({
 *     onPause: () => callStore.toggleVideo(),       // pause local
 *     onResume: () => callStore.toggleVideo(),      // restore local
 *     onBannerChange: (state) => uiStore.setBanner(state),
 *   });
 *   callEngine.qualityController.onChange((e) => adapter.feed(e));
 *   adapter.start();
 *
 * The wiring point in this codebase is a small mount component
 * (``NetworkAdaptiveVideoMount``) that subscribes to the call store
 * and calls ``feed()``. See the sibling .tsx file.
 */

export type AdaptiveQualityLevel =
  | 'excellent' | 'good' | 'fair' | 'poor' | 'critical' | 'unknown';

export interface AdaptiveQualityEvent {
  overallLevel: AdaptiveQualityLevel;
}

export interface AdaptiveBannerState {
  active: boolean;
  reason: 'poor-network' | null;
  lastChangeAt: number;
}

export interface AdaptiveOptions {
  /** Milliseconds of sustained "poor" before we pause. */
  sustainedMs?: number;
  /** Milliseconds of "good"+ before we resume. */
  recoverMs?: number;
  /** Action callbacks — provided by the caller. */
  onPause: () => void;
  onResume: () => void;
  onBannerChange?: (s: AdaptiveBannerState) => void;
}

export class NetworkAdaptiveVideo {
  private opts: Required<Omit<AdaptiveOptions, 'onBannerChange'>> & {
    onBannerChange: (s: AdaptiveBannerState) => void;
  };
  private autoPaused = false;
  private userOverride = false;
  private firstPoorAt: number | null = null;
  private firstRecoverAt: number | null = null;
  private banner: AdaptiveBannerState = {
    active: false,
    reason: null,
    lastChangeAt: 0,
  };

  constructor(opts: AdaptiveOptions) {
    this.opts = {
      sustainedMs: 4000,
      recoverMs: 6000,
      onBannerChange: () => {},
      ...opts,
    };
  }

  /** Feed a quality event in; the adapter decides whether to act. */
  feed(event: AdaptiveQualityEvent): void {
    if (this.userOverride) return;

    const level = event.overallLevel;
    const now = Date.now();
    const isPoor = level === 'poor' || level === 'critical';
    const isHealthy = level === 'good' || level === 'excellent';

    if (isPoor && !this.autoPaused) {
      if (this.firstPoorAt == null) this.firstPoorAt = now;
      if (now - this.firstPoorAt >= this.opts.sustainedMs) {
        this.autoPaused = true;
        this.firstPoorAt = null;
        this.firstRecoverAt = null;
        this.opts.onPause();
        this.setBanner({
          active: true, reason: 'poor-network',
          lastChangeAt: now,
        });
      }
      return;
    }

    if (!isPoor) {
      this.firstPoorAt = null;
    }

    if (isHealthy && this.autoPaused) {
      if (this.firstRecoverAt == null) this.firstRecoverAt = now;
      if (now - this.firstRecoverAt >= this.opts.recoverMs) {
        this.autoPaused = false;
        this.firstRecoverAt = null;
        this.opts.onResume();
        this.setBanner({
          active: false, reason: null, lastChangeAt: now,
        });
      }
    } else if (!isHealthy) {
      this.firstRecoverAt = null;
    }
  }

  /** The user manually toggled video. If we'd auto-paused, treat
   *  that as a hard override — stop adapting for the rest of this
   *  call. The banner is dismissed too. */
  noteManualVideoToggle(): void {
    if (this.autoPaused || this.banner.active) {
      this.userOverride = true;
      this.autoPaused = false;
      this.setBanner({
        active: false, reason: null,
        lastChangeAt: Date.now(),
      });
    }
  }

  /** Reset all state — call when a new call starts. */
  reset(): void {
    this.autoPaused = false;
    this.userOverride = false;
    this.firstPoorAt = null;
    this.firstRecoverAt = null;
    this.setBanner({
      active: false, reason: null, lastChangeAt: Date.now(),
    });
  }

  bannerState(): AdaptiveBannerState {
    return { ...this.banner };
  }

  private setBanner(next: AdaptiveBannerState): void {
    this.banner = next;
    this.opts.onBannerChange(next);
  }
}
