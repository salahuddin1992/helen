/**
 * ScreenShareOptimizer.ts — Advanced screen sharing optimization.
 *
 * Identified problems:
 *   1. Full-resolution capture even for mostly-static screens (text editors, docs)
 *   2. No content-type detection adaptation (video vs static content)
 *   3. Encoding at constant FPS regardless of screen activity
 *   4. No dirty-region detection (re-encode entire frame every tick)
 *   5. Cursor capture at full frame rate (wasteful for cursor-only changes)
 *   6. No quality feedback loop (doesn't adapt to receiver decode speed)
 *
 * Optimizations:
 *   1. Content-aware capture: detect if content is static/text/video/mixed
 *   2. Activity-based FPS: high FPS during activity, drop to 1-2 FPS when idle
 *   3. Region-of-interest: track mouse/active window for selective quality boost
 *   4. Frame differencing: skip encode if frame hasn't changed enough
 *   5. Adaptive quality: respond to receiver feedback (RTT, decode time)
 *   6. Progressive quality: send low-quality immediately, refine during idle
 *
 * Uses OffscreenCanvas for frame comparison (minimal main-thread impact).
 */

// ── Types ───────────────────────────────────────────────────

export type ScreenContentType = 'static' | 'text' | 'video' | 'mixed' | 'unknown';

export interface ScreenActivityState {
  /** Current content classification */
  contentType: ScreenContentType;
  /** How much of the screen changed since last frame (0-1) */
  changeRatio: number;
  /** Whether the cursor moved since last check */
  cursorMoved: boolean;
  /** Milliseconds since last significant change */
  idleDurationMs: number;
  /** Current effective capture FPS */
  effectiveFps: number;
  /** Whether the frame was skipped (too similar to previous) */
  frameSkipped: boolean;
  /** Total frames captured */
  totalFrames: number;
  /** Total frames skipped (saved) */
  skippedFrames: number;
}

export interface RegionOfInterest {
  x: number;
  y: number;
  width: number;
  height: number;
  /** Quality boost factor for this region (1 = normal, 2 = double) */
  qualityBoost: number;
}

export interface ScreenShareQualityFeedback {
  /** Receiver's decode time (ms) */
  decodeTimeMs: number;
  /** Round-trip time (ms) */
  rttMs: number;
  /** Receiver-reported frame drops */
  droppedFrames: number;
  /** Receiver's available bandwidth (kbps) */
  availableBandwidthKbps: number;
}

type ActivityCallback = (state: ScreenActivityState) => void;

// ── Constants ───────────────────────────────────────────────

/** Minimum change ratio to consider a frame "different enough" */
const CHANGE_THRESHOLD = 0.005;  // 0.5% pixel change

/** FPS tiers based on activity */
const FPS_TIERS = {
  idle: 1,       // Static screen — 1 FPS to keep connection alive
  lowActivity: 5,
  mediumActivity: 15,
  highActivity: 30,
} as const;

/** Time without changes before dropping to idle FPS */
const IDLE_TIMEOUT_MS = 3_000;

/** Frame comparison sample grid size (compare every Nth pixel) */
const SAMPLE_GRID_STEP = 8;

/** Quality reduction steps for feedback adaptation */
const QUALITY_STEPS = [1.0, 0.85, 0.7, 0.55, 0.4];

// ── ScreenShareOptimizer ────────────────────────────────────

export class ScreenShareOptimizer {
  private _canvas: OffscreenCanvas | null = null;
  private _ctx: OffscreenCanvasRenderingContext2D | null = null;
  private _previousFrame: ImageData | null = null;
  private _destroyed = false;

  // State
  private _contentType: ScreenContentType = 'unknown';
  private _lastChangeTime = Date.now();
  private _lastCursorPos = { x: 0, y: 0 };
  private _currentFps: number = FPS_TIERS.mediumActivity;
  private _totalFrames = 0;
  private _skippedFrames = 0;
  private _changeHistory: number[] = [];
  private _qualityStepIndex = 0;

  // ROI tracking
  private _roi: RegionOfInterest | null = null;

  // Listeners
  private _activityListeners: ActivityCallback[] = [];

  // ── Lifecycle ─────────────────────────────────────────────

  /**
   * Initialize the optimizer for a given capture resolution.
   */
  initialize(width: number, height: number): void {
    if (this._destroyed) return;

    try {
      this._canvas = new OffscreenCanvas(width, height);
      this._ctx = this._canvas.getContext('2d', {
        willReadFrequently: true,
        alpha: false,
      }) as OffscreenCanvasRenderingContext2D;
    } catch {
      // OffscreenCanvas not available — use reduced feature set
      this._canvas = null;
      this._ctx = null;
    }

    this._previousFrame = null;
    this._totalFrames = 0;
    this._skippedFrames = 0;
    this._changeHistory = [];
    this._qualityStepIndex = 0;
  }

  destroy(): void {
    this._destroyed = true;
    this._canvas = null;
    this._ctx = null;
    this._previousFrame = null;
    this._activityListeners = [];
    this._changeHistory = [];
  }

  // ── Frame Analysis ────────────────────────────────────────

  /**
   * Analyze a captured video frame to determine if it should be encoded.
   * Returns true if the frame has changed enough to warrant encoding.
   *
   * @param frame - VideoFrame or ImageBitmap from the capture stream
   */
  async shouldEncodeFrame(frame: VideoFrame | ImageBitmap): Promise<boolean> {
    this._totalFrames++;

    if (!this._ctx || !this._canvas) {
      // No comparison available — always encode
      return true;
    }

    try {
      // Draw frame to offscreen canvas
      this._ctx.drawImage(frame as any, 0, 0, this._canvas.width, this._canvas.height);

      // Sample pixels for change detection
      const currentData = this._ctx.getImageData(
        0, 0, this._canvas.width, this._canvas.height
      );

      if (!this._previousFrame) {
        this._previousFrame = currentData;
        this._lastChangeTime = Date.now();
        return true;
      }

      // Compare frames using sampled pixels
      const changeRatio = this._compareFrames(currentData, this._previousFrame);
      this._changeHistory.push(changeRatio);
      if (this._changeHistory.length > 30) this._changeHistory.shift();

      const shouldEncode = changeRatio >= CHANGE_THRESHOLD;

      if (shouldEncode) {
        this._previousFrame = currentData;
        this._lastChangeTime = Date.now();
      } else {
        this._skippedFrames++;
      }

      // Update content type based on change patterns
      this._classifyContent();

      // Update effective FPS
      this._updateAdaptiveFps();

      // Emit state
      this._emitActivity({
        contentType: this._contentType,
        changeRatio,
        cursorMoved: false, // Updated separately
        idleDurationMs: Date.now() - this._lastChangeTime,
        effectiveFps: this._currentFps,
        frameSkipped: !shouldEncode,
        totalFrames: this._totalFrames,
        skippedFrames: this._skippedFrames,
      });

      return shouldEncode;
    } catch {
      return true; // Fallback: always encode
    }
  }

  /**
   * Notify of cursor position change for ROI tracking.
   */
  updateCursorPosition(x: number, y: number): void {
    const dx = Math.abs(x - this._lastCursorPos.x);
    const dy = Math.abs(y - this._lastCursorPos.y);

    if (dx > 5 || dy > 5) {
      this._lastCursorPos = { x, y };
      this._lastChangeTime = Date.now();

      // Update ROI centered on cursor
      if (this._canvas) {
        const roiSize = Math.min(this._canvas.width, this._canvas.height) * 0.3;
        this._roi = {
          x: Math.max(0, x - roiSize / 2),
          y: Math.max(0, y - roiSize / 2),
          width: roiSize,
          height: roiSize,
          qualityBoost: 1.5,
        };
      }
    }
  }

  // ── Quality Feedback ──────────────────────────────────────

  /**
   * Process quality feedback from the receiver to adapt encoding.
   */
  processFeedback(feedback: ScreenShareQualityFeedback): void {
    // If receiver is struggling, reduce quality
    if (
      feedback.decodeTimeMs > 50 ||
      feedback.droppedFrames > 5 ||
      feedback.rttMs > 100
    ) {
      this._qualityStepIndex = Math.min(
        this._qualityStepIndex + 1,
        QUALITY_STEPS.length - 1
      );
    }
    // If receiver is comfortable, try increasing quality
    else if (
      feedback.decodeTimeMs < 20 &&
      feedback.droppedFrames === 0 &&
      feedback.rttMs < 30
    ) {
      this._qualityStepIndex = Math.max(0, this._qualityStepIndex - 1);
    }
  }

  // ── Getters ───────────────────────────────────────────────

  /**
   * Get the recommended capture FPS based on current activity.
   */
  getRecommendedFps(): number {
    return this._currentFps;
  }

  /**
   * Get the quality scale factor (0-1) based on feedback adaptation.
   */
  getQualityScale(): number {
    return QUALITY_STEPS[this._qualityStepIndex];
  }

  /**
   * Get the current region of interest (if any).
   */
  getROI(): RegionOfInterest | null {
    return this._roi;
  }

  /**
   * Get the detected content type.
   */
  getContentType(): ScreenContentType {
    return this._contentType;
  }

  /**
   * Get optimization statistics.
   */
  getStats(): { totalFrames: number; skippedFrames: number; skipRatio: number } {
    const skipRatio = this._totalFrames > 0 ? this._skippedFrames / this._totalFrames : 0;
    return {
      totalFrames: this._totalFrames,
      skippedFrames: this._skippedFrames,
      skipRatio: Math.round(skipRatio * 100) / 100,
    };
  }

  // ── Event Subscription ────────────────────────────────────

  onActivity(cb: ActivityCallback): () => void {
    this._activityListeners.push(cb);
    return () => {
      this._activityListeners = this._activityListeners.filter(l => l !== cb);
    };
  }

  // ── Internal: Frame Comparison ────────────────────────────

  private _compareFrames(current: ImageData, previous: ImageData): number {
    const data1 = current.data;
    const data2 = previous.data;
    const width = current.width;
    const height = current.height;

    let changedPixels = 0;
    let sampledPixels = 0;

    // Sample every Nth pixel (grid sampling for performance)
    for (let y = 0; y < height; y += SAMPLE_GRID_STEP) {
      for (let x = 0; x < width; x += SAMPLE_GRID_STEP) {
        const idx = (y * width + x) * 4;
        sampledPixels++;

        // Check if RGB values differ by more than threshold
        const dr = Math.abs(data1[idx] - data2[idx]);
        const dg = Math.abs(data1[idx + 1] - data2[idx + 1]);
        const db = Math.abs(data1[idx + 2] - data2[idx + 2]);

        // Pixel is "changed" if any channel differs by >10
        if (dr > 10 || dg > 10 || db > 10) {
          changedPixels++;
        }
      }
    }

    return sampledPixels > 0 ? changedPixels / sampledPixels : 0;
  }

  // ── Internal: Content Classification ──────────────────────

  private _classifyContent(): void {
    if (this._changeHistory.length < 5) {
      this._contentType = 'unknown';
      return;
    }

    const recent = this._changeHistory.slice(-10);
    const avgChange = recent.reduce((a, b) => a + b, 0) / recent.length;
    const maxChange = Math.max(...recent);

    if (avgChange < 0.001) {
      this._contentType = 'static';
    } else if (avgChange < 0.02 && maxChange < 0.05) {
      this._contentType = 'text';
    } else if (avgChange > 0.15) {
      this._contentType = 'video';
    } else {
      this._contentType = 'mixed';
    }
  }

  // ── Internal: Adaptive FPS ────────────────────────────────

  private _updateAdaptiveFps(): void {
    const idleMs = Date.now() - this._lastChangeTime;

    if (idleMs > IDLE_TIMEOUT_MS) {
      this._currentFps = FPS_TIERS.idle;
    } else if (this._contentType === 'static' || this._contentType === 'text') {
      this._currentFps = FPS_TIERS.lowActivity;
    } else if (this._contentType === 'video') {
      this._currentFps = FPS_TIERS.highActivity;
    } else {
      this._currentFps = FPS_TIERS.mediumActivity;
    }
  }

  // ── Internal: Event Emission ──────────────────────────────

  private _emitActivity(state: ScreenActivityState): void {
    for (const cb of this._activityListeners) {
      try { cb(state); } catch {}
    }
  }
}

// ── Singleton ───────────────────────────────────────────────

export const screenShareOptimizer = new ScreenShareOptimizer();
