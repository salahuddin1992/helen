/**
 * ScreenShareScaler — Adaptive screen capture quality.
 *
 * Screen sharing is bandwidth-heavy. This scaler adapts:
 *   - Capture resolution (1080p → 720p → 480p → 360p)
 *   - Frame rate (30 → 15 → 10 → 5 → 3 fps)
 *   - Content type detection (motion vs static → dynamic fps)
 *
 * Content-Aware Strategy:
 *   - Static content (documents, code): Low fps (3-5), high resolution
 *   - Mixed content (slides, browser): Medium fps (10-15), medium resolution
 *   - Motion content (video playback): Higher fps (15-30), lower resolution
 *
 * Detection works by comparing frame differences:
 *   - Low pixel change between frames → static → reduce fps
 *   - High pixel change → motion → maintain fps, reduce resolution
 *
 * Integrates with GracefulDegradationEngine for bandwidth ceiling.
 */

import { type DegradationLevel } from './GracefulDegradationEngine';

// ── Types ──────────────────────────────────────────────

export type ContentType = 'static' | 'mixed' | 'motion' | 'unknown';

export interface ScreenShareProfile {
  width: number;
  height: number;
  maxFps: number;
  maxBitrateKbps: number;
  contentType: ContentType;
}

export interface ScreenShareMetrics {
  currentWidth: number;
  currentHeight: number;
  currentFps: number;
  currentBitrateKbps: number;
  detectedContentType: ContentType;
  frameChangeRatio: number;   // 0-1, how much changes between frames
  cpuEncodeTimeMs: number;     // time to encode one frame
}

// ── Presets ─────────────────────────────────────────────

const SCREEN_PRESETS: Record<string, ScreenShareProfile> = {
  'full': {
    width: 1920, height: 1080, maxFps: 30, maxBitrateKbps: 6_000,
    contentType: 'unknown',
  },
  'high': {
    width: 1920, height: 1080, maxFps: 15, maxBitrateKbps: 4_000,
    contentType: 'mixed',
  },
  'medium': {
    width: 1280, height: 720, maxFps: 10, maxBitrateKbps: 2_000,
    contentType: 'mixed',
  },
  'low': {
    width: 854, height: 480, maxFps: 5, maxBitrateKbps: 800,
    contentType: 'static',
  },
  'minimal': {
    width: 640, height: 360, maxFps: 3, maxBitrateKbps: 400,
    contentType: 'static',
  },
};

// Content-adaptive profiles
const CONTENT_PROFILES: Record<ContentType, { fpsMultiplier: number; resolutionMultiplier: number }> = {
  static: { fpsMultiplier: 0.3, resolutionMultiplier: 1.0 },   // Low fps, full resolution
  mixed:  { fpsMultiplier: 0.6, resolutionMultiplier: 0.8 },
  motion: { fpsMultiplier: 1.0, resolutionMultiplier: 0.6 },   // High fps, lower resolution
  unknown: { fpsMultiplier: 0.5, resolutionMultiplier: 0.9 },
};

// ── Change Detection ───────────────────────────────────

const STATIC_THRESHOLD = 0.02;    // <2% pixel change → static
const MOTION_THRESHOLD = 0.15;    // >15% pixel change → motion
const DETECTION_INTERVAL_MS = 2_000;
const DETECTION_CANVAS_SIZE = 160; // Downsample to 160px wide for comparison

// ── Scaler Implementation ──────────────────────────────

export class ScreenShareScaler {
  private _currentPreset = 'high';
  private _degradationLevel: DegradationLevel = 0;
  private _detectedContent: ContentType = 'unknown';
  private _frameChangeRatio = 0;

  // Frame comparison state
  private _prevFrameData: Uint8ClampedArray | null = null;
  private _comparisonCanvas: HTMLCanvasElement | null = null;
  private _comparisonCtx: CanvasRenderingContext2D | null = null;
  private _detectionTimer: ReturnType<typeof setInterval> | null = null;
  private _videoElement: HTMLVideoElement | null = null;

  private _destroyed = false;

  // ── Configuration ─────────────────────────────────────

  setDegradationLevel(level: DegradationLevel): void {
    this._degradationLevel = level;
    this._recomputePreset();
  }

  // ── Get Current Profile ───────────────────────────────

  getCurrentProfile(): ScreenShareProfile {
    const preset = SCREEN_PRESETS[this._currentPreset];
    const contentMod = CONTENT_PROFILES[this._detectedContent];

    return {
      width: Math.round(preset.width * contentMod.resolutionMultiplier),
      height: Math.round(preset.height * contentMod.resolutionMultiplier),
      maxFps: Math.max(3, Math.round(preset.maxFps * contentMod.fpsMultiplier)),
      maxBitrateKbps: preset.maxBitrateKbps,
      contentType: this._detectedContent,
    };
  }

  getMetrics(): ScreenShareMetrics {
    const profile = this.getCurrentProfile();
    return {
      currentWidth: profile.width,
      currentHeight: profile.height,
      currentFps: profile.maxFps,
      currentBitrateKbps: profile.maxBitrateKbps,
      detectedContentType: this._detectedContent,
      frameChangeRatio: this._frameChangeRatio,
      cpuEncodeTimeMs: 0,
    };
  }

  // ── Capture Constraints ───────────────────────────────

  /**
   * Returns MediaStreamConstraints for getDisplayMedia / getUserMedia
   * that respect the current profile.
   */
  getCaptureConstraints(sourceId?: string): MediaStreamConstraints {
    const profile = this.getCurrentProfile();

    const videoConstraints: any = {
      width: { ideal: profile.width, max: profile.width },
      height: { ideal: profile.height, max: profile.height },
      frameRate: { ideal: profile.maxFps, max: profile.maxFps },
    };

    // Electron desktop capture requires special mandatory constraints
    if (sourceId) {
      return {
        audio: false,
        video: {
          mandatory: {
            chromeMediaSource: 'desktop',
            chromeMediaSourceId: sourceId,
            maxWidth: profile.width,
            maxHeight: profile.height,
            maxFrameRate: profile.maxFps,
          },
        } as any,
      };
    }

    return {
      audio: false,
      video: videoConstraints,
    };
  }

  /**
   * Returns RTCRtpEncodingParameters for applying to the screen share sender.
   */
  getEncodingParams(): RTCRtpEncodingParameters {
    const profile = this.getCurrentProfile();
    return {
      maxBitrate: profile.maxBitrateKbps * 1000,
      maxFramerate: profile.maxFps,
      scaleResolutionDownBy: 1.0,
    };
  }

  // ── Content Type Detection ────────────────────────────

  /**
   * Start content detection on the active screen share stream.
   * Periodically samples frames to detect motion level.
   */
  startContentDetection(stream: MediaStream): void {
    this.stopContentDetection();

    const videoTrack = stream.getVideoTracks()[0];
    if (!videoTrack) return;

    // Create a hidden video element to capture frames from
    this._videoElement = document.createElement('video');
    this._videoElement.srcObject = stream;
    this._videoElement.muted = true;
    this._videoElement.play().catch(() => {});

    // Comparison canvas
    this._comparisonCanvas = document.createElement('canvas');
    this._comparisonCanvas.width = DETECTION_CANVAS_SIZE;
    this._comparisonCanvas.height = Math.round(DETECTION_CANVAS_SIZE * 0.5625); // 16:9
    this._comparisonCtx = this._comparisonCanvas.getContext('2d', { willReadFrequently: true });

    this._detectionTimer = setInterval(() => this._detectContent(), DETECTION_INTERVAL_MS);
  }

  stopContentDetection(): void {
    if (this._detectionTimer) {
      clearInterval(this._detectionTimer);
      this._detectionTimer = null;
    }
    if (this._videoElement) {
      this._videoElement.srcObject = null;
      this._videoElement = null;
    }
    this._comparisonCanvas = null;
    this._comparisonCtx = null;
    this._prevFrameData = null;
  }

  destroy(): void {
    this._destroyed = true;
    this.stopContentDetection();
  }

  // ── Internal Detection ────────────────────────────────

  private _detectContent(): void {
    if (!this._videoElement || !this._comparisonCtx || !this._comparisonCanvas) return;
    if (this._videoElement.readyState < 2) return;

    const ctx = this._comparisonCtx;
    const w = this._comparisonCanvas.width;
    const h = this._comparisonCanvas.height;

    // Draw current frame (downsampled)
    ctx.drawImage(this._videoElement, 0, 0, w, h);
    const currentData = ctx.getImageData(0, 0, w, h).data;

    if (this._prevFrameData) {
      // Compare frames pixel-by-pixel
      let changedPixels = 0;
      const totalPixels = w * h;
      const threshold = 30; // RGB difference threshold per channel

      for (let i = 0; i < currentData.length; i += 4) {
        const rDiff = Math.abs(currentData[i] - this._prevFrameData[i]);
        const gDiff = Math.abs(currentData[i + 1] - this._prevFrameData[i + 1]);
        const bDiff = Math.abs(currentData[i + 2] - this._prevFrameData[i + 2]);

        if (rDiff > threshold || gDiff > threshold || bDiff > threshold) {
          changedPixels++;
        }
      }

      this._frameChangeRatio = changedPixels / totalPixels;

      // Classify content
      if (this._frameChangeRatio < STATIC_THRESHOLD) {
        this._detectedContent = 'static';
      } else if (this._frameChangeRatio > MOTION_THRESHOLD) {
        this._detectedContent = 'motion';
      } else {
        this._detectedContent = 'mixed';
      }
    }

    this._prevFrameData = new Uint8ClampedArray(currentData);
    this._recomputePreset();
  }

  private _recomputePreset(): void {
    if (this._degradationLevel >= 5) {
      this._currentPreset = 'minimal';
    } else if (this._degradationLevel >= 4) {
      this._currentPreset = 'minimal';
    } else if (this._degradationLevel >= 3) {
      this._currentPreset = 'low';
    } else if (this._degradationLevel >= 2) {
      this._currentPreset = 'medium';
    } else if (this._degradationLevel >= 1) {
      this._currentPreset = 'high';
    } else {
      this._currentPreset = 'full';
    }
  }
}
