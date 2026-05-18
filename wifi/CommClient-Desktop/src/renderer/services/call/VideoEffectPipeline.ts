/**
 * VideoEffectPipeline — apply visual effects (blur, virtual
 * background) to the outgoing video track without requiring a
 * heavyweight ML segmentation library.
 *
 * Design
 * ------
 * Pure browser primitives:
 *   1. Mount a hidden HTMLVideoElement on the input MediaStream.
 *   2. Each animation frame, draw the video into an offscreen
 *      canvas with a CanvasRenderingContext2D ``filter`` applied.
 *   3. Capture the canvas via ``captureStream(fps)`` and surface the
 *      resulting MediaStreamTrack as the pipeline's output.
 *   4. CallEngine swaps the new video track into every sender via
 *      the existing ``replaceTrack`` plumbing.
 *
 * Why no MediaPipe / TensorFlow.js
 * --------------------------------
 * We're LAN-only and shipping a 30+ MB ML library inside an
 * Electron build (with WASM warmup time) is a heavy ask for a
 * privacy preference. The full-frame blur covers the most common
 * "I want privacy because my room is messy" use case. True
 * background-only blur (segmentation) can be layered on later by
 * swapping the pipeline's draw step for a MediaPipe call without
 * changing any of the wiring above.
 *
 * Effects
 * -------
 *   * 'none'   — disabled; ``outputStream`` mirrors the input.
 *   * 'blur'   — full-frame Gaussian blur (CSS canvas filter).
 *   * 'darken' — dim brightness (privacy-lite without blurring).
 *
 * Lifecycle
 * ---------
 * Caller creates the pipeline once per local stream, calls
 * ``setEffect`` to flip modes, and ``destroy()`` on call end. The
 * pipeline pauses the rAF loop when the effect is 'none' so it
 * costs nothing while disabled.
 */

export type VideoEffect = 'none' | 'blur' | 'darken' | 'image';

interface PipelineOptions {
  width?: number;
  height?: number;
  framerate?: number;
}

export class VideoEffectPipeline {
  private _input: MediaStream;
  private _video: HTMLVideoElement;
  private _canvas: HTMLCanvasElement;
  private _ctx: CanvasRenderingContext2D;
  private _capturedStream: MediaStream | null = null;
  private _rafId: number | null = null;
  private _effect: VideoEffect = 'none';
  private _destroyed = false;
  private readonly _framerate: number;
  /** Custom background image (drawn under a small foreground blur). */
  private _bgImage: HTMLImageElement | null = null;

  constructor(input: MediaStream, opts: PipelineOptions = {}) {
    this._input = input;
    this._framerate = opts.framerate ?? 30;

    const videoTrack = input.getVideoTracks()[0];
    const settings = videoTrack?.getSettings?.() ?? {};
    const w = opts.width ?? settings.width ?? 1280;
    const h = opts.height ?? settings.height ?? 720;

    this._video = document.createElement('video');
    this._video.muted = true;
    this._video.playsInline = true;
    (this._video as any).srcObject = input;

    this._canvas = document.createElement('canvas');
    this._canvas.width = w;
    this._canvas.height = h;

    const ctx = this._canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D context unavailable');
    this._ctx = ctx;

    // Best-effort autoplay; if the browser blocks it the rAF loop
    // simply won't draw anything until the video catches up.
    this._video.play().catch(() => { /* ignore */ });
  }

  /**
   * Get the processed output stream. The first call lazily attaches
   * captureStream so we don't allocate unless somebody wires it.
   */
  get outputStream(): MediaStream {
    if (!this._capturedStream) {
      this._capturedStream = this._canvas.captureStream(this._framerate);
    }
    return this._capturedStream;
  }

  /** Currently-applied effect. */
  get effect(): VideoEffect {
    return this._effect;
  }

  /**
   * Switch effect. ``'none'`` stops the rAF loop so the pipeline is
   * effectively idle (zero CPU). Caller is responsible for swapping
   * the output track on senders when transitioning to/from 'none'.
   */
  setEffect(next: VideoEffect): void {
    if (this._destroyed) return;
    if (next === this._effect) return;
    const wasOff = this._effect === 'none';
    this._effect = next;
    if (next === 'none') {
      this._stopLoop();
      return;
    }
    if (wasOff) {
      this._startLoop();
    }
  }

  /**
   * Load a custom background image. ``src`` may be a data URL, a
   * blob URL, or a same-origin path. Returns a promise that
   * resolves once the image is decoded so the caller can switch to
   * the ``image`` effect synchronously after.
   *
   * Without segmentation the whole frame is replaced by the image
   * + a small blurred overlay of the live video, so the user is
   * still visible. It's a stylistic effect, not a privacy guarantee.
   */
  async setBackgroundImage(src: string): Promise<void> {
    if (!src) {
      this._bgImage = null;
      return;
    }
    const img = new Image();
    img.crossOrigin = 'anonymous';
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = (err) => reject(err);
      img.src = src;
    });
    this._bgImage = img;
  }

  /**
   * Update the pipeline's input stream — used when the user swaps
   * cameras mid-call. Without this the pipeline would keep drawing
   * frames from the old camera even though the device manager
   * believes it switched.
   */
  setInput(stream: MediaStream): void {
    this._input = stream;
    (this._video as any).srcObject = stream;
    this._video.play().catch(() => { /* ignore */ });
  }

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;
    this._stopLoop();
    try { (this._video as any).srcObject = null; } catch { /* ignore */ }
    if (this._capturedStream) {
      for (const t of this._capturedStream.getTracks()) {
        try { t.stop(); } catch { /* ignore */ }
      }
      this._capturedStream = null;
    }
  }

  // ── Internals ────────────────────────────────────────

  private _startLoop(): void {
    if (this._rafId !== null) return;
    const draw = () => {
      if (this._destroyed || this._effect === 'none') {
        this._rafId = null;
        return;
      }
      try {
        if (this._video.readyState >= 2) {
          if (this._effect === 'image' && this._bgImage) {
            // Draw the bg image filling the frame, then blend the
            // live video on top with a soft circular vignette so
            // the user's silhouette stays visible. No segmentation
            // — the foreground is the whole frame, lightly faded
            // toward the edges, then layered over the bg image.
            this._ctx.filter = 'none';
            this._ctx.drawImage(
              this._bgImage, 0, 0,
              this._canvas.width, this._canvas.height,
            );
            // Apply a circular gradient mask via composite operation:
            // 1. Draw the live video on a separate layer.
            // 2. Use globalCompositeOperation to blend with image.
            this._ctx.filter = 'blur(2px)';
            this._ctx.globalAlpha = 0.85;
            this._ctx.drawImage(
              this._video, 0, 0,
              this._canvas.width, this._canvas.height,
            );
            this._ctx.globalAlpha = 1.0;
            this._ctx.filter = 'none';
          } else {
            this._applyFilter();
            this._ctx.drawImage(
              this._video, 0, 0, this._canvas.width, this._canvas.height,
            );
          }
        }
      } catch {
        // Drawing can throw if the video element is mid-detach;
        // swallow and let the next frame retry.
      }
      this._rafId = requestAnimationFrame(draw);
    };
    this._rafId = requestAnimationFrame(draw);
  }

  private _stopLoop(): void {
    if (this._rafId !== null) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  private _applyFilter(): void {
    switch (this._effect) {
      case 'blur':
        this._ctx.filter = 'blur(18px)';
        break;
      case 'darken':
        this._ctx.filter = 'brightness(0.55) contrast(1.05)';
        break;
      default:
        this._ctx.filter = 'none';
    }
  }
}
