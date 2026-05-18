/**
 * ScreenShareManager — dedicated screen sharing orchestrator.
 *
 * Handles the complete screen sharing lifecycle beyond basic track management:
 *
 *   - LAN-optimized quality presets for screen content (high-res, tuned fps)
 *   - Dual-track mode: simultaneous camera + screen on separate RTP streams
 *   - Replace-camera mode: swap camera video for screen feed
 *   - Auto-recovery on track failure or OS-level stop
 *   - Permission state machine with retry
 *   - Source change without full stop/start cycle
 *   - Cursor capture configuration
 *   - Content hint optimization (detail vs. motion)
 *
 * Works with PeerConnection (1-to-1) and GroupCallManager (mesh).
 */

import { PeerConnection } from './PeerConnection';
import { GroupCallManager } from './GroupCallManager';

// ── Types ───────────────────────────────────────────

export type ScreenShareMode = 'dual-track' | 'replace-camera';
export type ContentType = 'screen' | 'window' | 'tab';
export type ContentHint = 'detail' | 'motion' | 'text';

export interface ScreenShareSource {
  id: string;
  name: string;
  type: ContentType;
  thumbnail?: string;
  displayId?: string;
}

export interface ScreenShareQualityPreset {
  label: string;
  maxWidth: number;
  maxHeight: number;
  maxFrameRate: number;
  /** Bitrate in kbps for the screen track */
  bitrateKbps: number;
  /** Content hint for encoder optimization */
  contentHint: ContentHint;
}

/**
 * LAN-optimized presets: bandwidth is not a constraint on local networks,
 * so we favor resolution and clarity over compression.
 */
export const SCREEN_QUALITY_PRESETS: Record<string, ScreenShareQualityPreset> = {
  'lan-max': {
    label: 'LAN Max (4K/30)',
    maxWidth: 3840,
    maxHeight: 2160,
    maxFrameRate: 30,
    bitrateKbps: 20_000,
    contentHint: 'detail',
  },
  '1080p': {
    label: '1080p / 60fps',
    maxWidth: 1920,
    maxHeight: 1080,
    maxFrameRate: 60,
    bitrateKbps: 12_000,
    contentHint: 'detail',
  },
  '1080p-detail': {
    label: '1080p Detail (text/code)',
    maxWidth: 1920,
    maxHeight: 1080,
    maxFrameRate: 15,
    bitrateKbps: 8_000,
    contentHint: 'text',
  },
  '720p': {
    label: '720p / 30fps',
    maxWidth: 1280,
    maxHeight: 720,
    maxFrameRate: 30,
    bitrateKbps: 5_000,
    contentHint: 'detail',
  },
  'motion': {
    label: 'Motion (video/demo)',
    maxWidth: 1920,
    maxHeight: 1080,
    maxFrameRate: 60,
    bitrateKbps: 15_000,
    contentHint: 'motion',
  },
};

export type ScreenShareStatus =
  | 'idle'
  | 'requesting-source'
  | 'acquiring'
  | 'active'
  | 'paused'
  | 'error'
  | 'stopping';

export interface ScreenShareState {
  status: ScreenShareStatus;
  mode: ScreenShareMode;
  source: ScreenShareSource | null;
  preset: string;
  stream: MediaStream | null;
  /** Separate camera stream in dual-track mode */
  cameraStream: MediaStream | null;
  error: string | null;
  startedAt: number | null;
}

export interface ScreenShareCallbacks {
  onStateChange: (state: ScreenShareState) => void;
  onSourceEnded: () => void;
  onError: (error: string) => void;
}

export interface ScreenShareMetrics {
  duration: number;
  sourceSwitches: number;
  qualityChanges: number;
  pauseCount: number;
  avgBitrate: number;
  peakBitrate: number;
  framesDropped: number;
  resolution: { width: number; height: number };
}

// ── Manager ─────────────────────────────────────────

export class ScreenShareManager {
  private callbacks: ScreenShareCallbacks;
  private _state: ScreenShareState;
  private _destroyed = false;

  // Track senders for clean removal
  private _screenSenders: Map<string, RTCRtpSender> = new Map();
  private _cameraSender: RTCRtpSender | null = null;

  // References to active connections
  private _peerConnection: PeerConnection | null = null;
  private _groupManager: GroupCallManager | null = null;

  // Saved camera track for restore on stop (replace-camera mode)
  private _savedCameraTrack: MediaStreamTrack | null = null;

  // ── System audio capture ──────────────────────────
  private _hasAudioTrack = false;

  // ── Adaptive quality ──────────────────────────────
  private _adaptiveQualityEnabled = false;
  private _adaptiveQualityTimer: NodeJS.Timeout | null = null;
  private _currentEffectiveBitrate = 0;

  // ── Metrics tracking ─────────────────────────────
  private _metricsData: ScreenShareMetrics = {
    duration: 0,
    sourceSwitches: 0,
    qualityChanges: 0,
    pauseCount: 0,
    avgBitrate: 0,
    peakBitrate: 0,
    framesDropped: 0,
    resolution: { width: 0, height: 0 },
  };
  private _metricsStartTime: number | null = null;
  private _metricsSamples: number[] = [];

  // ── Concurrent operation lock ─────────────────────
  private _operationLock = false;
  private _lockQueue: Promise<void> = Promise.resolve();

  constructor(callbacks: ScreenShareCallbacks) {
    this.callbacks = callbacks;
    this._state = {
      status: 'idle',
      mode: 'dual-track',
      source: null,
      preset: '1080p',
      stream: null,
      cameraStream: null,
      error: null,
      startedAt: null,
    };
  }

  get state(): ScreenShareState {
    return { ...this._state };
  }

  get isSharing(): boolean {
    return this._state.status === 'active' || this._state.status === 'paused';
  }

  /**
   * Attach to a single peer connection (1-to-1 call).
   */
  attachPeer(pc: PeerConnection): void {
    this._peerConnection = pc;
    this._groupManager = null;
  }

  /**
   * Attach to a group call manager (mesh call).
   */
  attachGroup(gm: GroupCallManager): void {
    this._groupManager = gm;
    this._peerConnection = null;
  }

  /**
   * Proper async mutex lock using promise queue.
   * Replaces the buggy busy-wait pattern.
   */
  private async _withLock<T>(fn: () => Promise<T>): Promise<T> {
    let release: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const prev = this._lockQueue;
    this._lockQueue = gate;
    await prev;
    try {
      return await fn();
    } finally {
      release!();
    }
  }

  // ── Start / Stop ──────────────────────────────────

  /**
   * Start screen sharing with a selected source.
   *
   * @param source — the Electron desktop source to capture
   * @param mode — 'dual-track' adds screen alongside camera,
   *               'replace-camera' swaps camera for screen
   * @param preset — quality preset name from SCREEN_QUALITY_PRESETS
   * @param localStream — current camera stream (needed for replace-camera mode)
   */
  async start(
    source: ScreenShareSource,
    mode: ScreenShareMode = 'dual-track',
    preset: string = '1080p',
    localStream?: MediaStream | null,
  ): Promise<MediaStream> {
    return this._withLock(async () => {
      if (this._destroyed) throw new Error('ScreenShareManager is destroyed');
      if (this._state.status === 'active') {
        // Already sharing — switch source instead
        return this.switchSource(source, preset);
      }

      this._updateState({
        status: 'acquiring',
        mode,
        source,
        preset,
        error: null,
      });

      const qualityPreset = SCREEN_QUALITY_PRESETS[preset] || SCREEN_QUALITY_PRESETS['1080p'];

      try {
        // Acquire screen stream via Electron chromeMediaSource
        const stream = await this._acquireScreenStream(source.id, qualityPreset);
        const screenTrack = stream.getVideoTracks()[0];

        if (!screenTrack) {
          throw new Error('No video track from screen capture');
        }

        // Set content hint for encoder optimization
        if ('contentHint' in screenTrack) {
          (screenTrack as any).contentHint = qualityPreset.contentHint;
        }

        // Handle OS-level share stop (user clicks "Stop sharing" in OS UI)
        screenTrack.onended = () => {
          console.log('[ScreenShare] Track ended by OS');
          this._handleTrackEnded();
        };

        // Route based on mode
        if (mode === 'dual-track') {
          await this._startDualTrack(screenTrack, stream, qualityPreset);
        } else {
          await this._startReplaceCamera(screenTrack, stream, localStream, qualityPreset);
        }

        this._updateState({
          status: 'active',
          stream,
          startedAt: Date.now(),
        });

        // Initialize metrics tracking
        this._initializeMetrics(screenTrack);

        console.log(
          `[ScreenShare] Started: mode=${mode}, source="${source.name}", preset=${preset}`
        );

        return stream;
      } catch (e: any) {
        const errorMsg = this._classifyError(e);
        this._updateState({
          status: 'error',
          error: errorMsg,
          stream: null,
        });
        this.callbacks.onError(errorMsg);
        throw e;
      }
    });
  }

  /**
   * Stop screen sharing and clean up.
   *
   * Every cleanup step is isolated in its own try/catch so a failure
   * in one (e.g. a sender already gone after a peer left) doesn't
   * skip the rest. Without this, an early-step failure used to leave
   * the screen track running, the adaptive timer firing, and the
   * camera unrestored — all visible to the user as "Stop Sharing
   * doesn't actually stop anything".
   */
  async stop(): Promise<void> {
    return this._withLock(async () => {
      if (this._state.status === 'idle' || this._state.status === 'stopping') return;

      this._updateState({ status: 'stopping' });

      // 1. Remove screen track from the 1:1 peer connection.
      try {
        if (this._peerConnection && !this._peerConnection.destroyed) {
          const sender = this._screenSenders.get('single');
          if (sender) {
            this._peerConnection.removeScreenTrack(sender);
          }
        }
      } catch (e) {
        console.warn('[ScreenShare] removeScreenTrack(single) failed:', e);
      }

      // 2. Remove screen track from every group peer.
      try {
        if (this._groupManager && !this._groupManager.destroyed) {
          this._groupManager.removeScreenTrackAll(this._screenSenders);
        }
      } catch (e) {
        console.warn('[ScreenShare] removeScreenTrackAll failed:', e);
      }

      // 3. Restore camera track if we replaced it. This is the most
      //    failure-prone step — if it throws, we still want the
      //    teardown that follows to complete.
      try {
        if (this._state.mode === 'replace-camera' && this._savedCameraTrack) {
          await this._restoreCameraTrack();
        }
      } catch (e) {
        console.warn('[ScreenShare] restoreCameraTrack failed:', e);
      }

      // 4. Stop every track on the screen stream — critical for the
      //    OS to release the capture handle and the browser to drop
      //    the "you are sharing" indicator. Stop each track inside
      //    its own try so one bad track doesn't leak the others.
      try {
        if (this._state.stream) {
          for (const t of this._state.stream.getTracks()) {
            try { t.stop(); } catch (err) {
              console.debug('[ScreenShare] track.stop failed:', err);
            }
          }
        }
      } catch (e) {
        console.warn('[ScreenShare] track stop loop failed:', e);
      }

      // 5. Also stop the camera-stream we may have allocated for
      //    side-by-side mode (separate from the main local stream).
      try {
        if (this._state.cameraStream) {
          for (const t of this._state.cameraStream.getTracks()) {
            try { t.stop(); } catch { /* ignore */ }
          }
        }
      } catch (e) {
        console.warn('[ScreenShare] camera-stream stop failed:', e);
      }

      // 6. Stop adaptive quality monitoring timer.
      try {
        if (this._adaptiveQualityTimer) {
          clearInterval(this._adaptiveQualityTimer);
          this._adaptiveQualityTimer = null;
        }
      } catch { /* ignore */ }

      // 7. Final state reset — runs even when any earlier step
      //    threw, so we never get stuck in 'stopping'.
      this._screenSenders.clear();
      this._savedCameraTrack = null;
      this._hasAudioTrack = false;

      this._updateState({
        status: 'idle',
        source: null,
        stream: null,
        cameraStream: null,
        error: null,
        startedAt: null,
      });

      console.log('[ScreenShare] Stopped');
    });
  }

  // ── Source Switching ───────────────────────────────

  /**
   * Switch to a different source without full stop/start.
   * Replaces the track in-place via RTCRtpSender.replaceTrack().
   */
  async switchSource(
    newSource: ScreenShareSource,
    preset?: string,
  ): Promise<MediaStream> {
    return this._withLock(async () => {
      if (this._state.status !== 'active') {
        throw new Error('Cannot switch source — not currently sharing');
      }

      const qualityPreset = SCREEN_QUALITY_PRESETS[preset || this._state.preset]
        || SCREEN_QUALITY_PRESETS['1080p'];

      // Acquire new stream
      const newStream = await this._acquireScreenStream(newSource.id, qualityPreset);
      const newTrack = newStream.getVideoTracks()[0];

      if (!newTrack) throw new Error('No video track from new source');

      // Set content hint
      if ('contentHint' in newTrack) {
        (newTrack as any).contentHint = qualityPreset.contentHint;
      }

      newTrack.onended = () => this._handleTrackEnded();

      // Replace track in all peer connections (no renegotiation needed!)
      const replacePromises: Promise<void>[] = [];

      for (const [peerId, sender] of this._screenSenders) {
        replacePromises.push(
          sender.replaceTrack(newTrack).catch((e) => {
            console.warn(`[ScreenShare] replaceTrack failed for ${peerId}:`, e);
          })
        );
      }

      await Promise.allSettled(replacePromises);

      // Stop old stream
      if (this._state.stream) {
        this._state.stream.getTracks().forEach((t) => t.stop());
      }

      this._updateState({
        source: newSource,
        stream: newStream,
        preset: preset || this._state.preset,
      });

      this._metricsData.sourceSwitches++;

      console.log(`[ScreenShare] Source switched to "${newSource.name}"`);
      return newStream;
    });
  }

  // ── Pause / Resume ────────────────────────────────

  /**
   * Pause screen sharing (disable track, keeps connection).
   * Useful for brief privacy moments without full stop/start overhead.
   */
  pause(): void {
    if (this._state.status !== 'active') return;

    const track = this._state.stream?.getVideoTracks()[0];
    if (track) {
      track.enabled = false;
    }

    this._metricsData.pauseCount++;
    this._updateState({ status: 'paused' });
    console.log('[ScreenShare] Paused');
  }

  /**
   * Resume a paused screen share.
   */
  resume(): void {
    if (this._state.status !== 'paused') return;

    const track = this._state.stream?.getVideoTracks()[0];
    if (track) {
      track.enabled = true;
    }

    this._updateState({ status: 'active' });
    console.log('[ScreenShare] Resumed');
  }

  // ── Quality Controls ──────────────────────────────

  /**
   * Change screen share quality preset on-the-fly.
   */
  async setQuality(preset: string): Promise<void> {
    const qualityPreset = SCREEN_QUALITY_PRESETS[preset];
    if (!qualityPreset) {
      console.warn(`[ScreenShare] Unknown preset: ${preset}`);
      return;
    }

    // Apply bitrate to screen track senders
    for (const [, sender] of this._screenSenders) {
      try {
        const params = sender.getParameters();
        if (!params.encodings || params.encodings.length === 0) {
          params.encodings = [{}];
        }
        params.encodings[0].maxBitrate = qualityPreset.bitrateKbps * 1000;
        params.encodings[0].maxFramerate = qualityPreset.maxFrameRate;
        await sender.setParameters(params);
      } catch (e) {
        console.warn('[ScreenShare] setParameters failed:', e);
      }
    }

    this._metricsData.qualityChanges++;
    this._updateState({ preset });
    console.log(`[ScreenShare] Quality set to ${preset}`);
  }

  // ── System Audio Capture ────────────────────────────

  /**
   * Start screen sharing with system audio track.
   * Uses chromeMediaSource: 'desktop' with audio capture enabled.
   *
   * @param source — the Electron desktop source to capture
   * @param mode — 'dual-track' or 'replace-camera'
   * @param preset — quality preset name
   * @param localStream — current camera stream
   */
  async startWithAudio(
    source: ScreenShareSource,
    mode: ScreenShareMode,
    preset: string,
    localStream: MediaStream,
  ): Promise<MediaStream> {
    if (this._destroyed) throw new Error('ScreenShareManager is destroyed');

    const qualityPreset = SCREEN_QUALITY_PRESETS[preset] || SCREEN_QUALITY_PRESETS['1080p'];

    try {
      // Acquire screen + audio stream
      const stream = await this._acquireScreenStreamWithAudio(source.id, qualityPreset);
      const audioTracks = stream.getAudioTracks();

      if (audioTracks.length > 0) {
        this._hasAudioTrack = true;
        console.log('[ScreenShare] Audio track captured');
      }

      // Now proceed with normal start process
      const screenTrack = stream.getVideoTracks()[0];
      if (!screenTrack) {
        throw new Error('No video track from screen capture');
      }

      if ('contentHint' in screenTrack) {
        (screenTrack as any).contentHint = qualityPreset.contentHint;
      }

      screenTrack.onended = () => {
        console.log('[ScreenShare] Track ended by OS');
        this._handleTrackEnded();
      };

      if (mode === 'dual-track') {
        await this._startDualTrack(screenTrack, stream, qualityPreset);
      } else {
        await this._startReplaceCamera(screenTrack, stream, localStream, qualityPreset);
      }

      this._updateState({
        status: 'active',
        stream,
        mode,
        source,
        preset,
        startedAt: Date.now(),
      });

      this._initializeMetrics(screenTrack);
      console.log(`[ScreenShare] Started with audio: mode=${mode}, source="${source.name}"`);

      return stream;
    } catch (e: any) {
      const errorMsg = this._classifyError(e);
      this._updateState({
        status: 'error',
        error: errorMsg,
        stream: null,
      });
      this.callbacks.onError(errorMsg);
      throw e;
    }
  }

  /**
   * Check if audio track is being captured.
   */
  isAudioCaptured(): boolean {
    return this._hasAudioTrack;
  }

  // ── Adaptive Quality ────────────────────────────────

  /**
   * Enable or disable adaptive quality monitoring.
   * When enabled, monitors bitrate every 3 seconds and automatically
   * downgrades quality if bitrate drops below 60% of target.
   */
  enableAdaptiveQuality(enabled: boolean): void {
    if (enabled === this._adaptiveQualityEnabled) return;

    if (enabled) {
      this._adaptiveQualityEnabled = true;
      // Clear any existing timer before creating a new one
      if (this._adaptiveQualityTimer) {
        clearInterval(this._adaptiveQualityTimer);
      }
      this._adaptiveQualityTimer = setInterval(() => {
        this._monitorBitrateForAdaptation();
      }, 3000);
      console.log('[ScreenShare] Adaptive quality enabled');
    } else {
      this._adaptiveQualityEnabled = false;
      if (this._adaptiveQualityTimer) {
        clearInterval(this._adaptiveQualityTimer);
        this._adaptiveQualityTimer = null;
      }
      console.log('[ScreenShare] Adaptive quality disabled');
    }
  }

  /**
   * Monitor bitrate stats and auto-degrade quality if needed.
   * Private method called by adaptive quality timer.
   */
  private async _monitorBitrateForAdaptation(): Promise<void> {
    if (!this._peerConnection?.peerConnection) return;

    try {
      const stats = await this._peerConnection.peerConnection.getStats();
      let totalBitrate = 0;

      stats.forEach((report) => {
        if (report.type === 'outbound-rtp' && report.kind === 'video') {
          const bitrate = ((report.bytesSent || 0) * 8) / 3; // bits per second over 3s interval
          totalBitrate += bitrate;
        }
      });

      this._currentEffectiveBitrate = Math.round(totalBitrate / 1000); // Kbps
      this._metricsSamples.push(this._currentEffectiveBitrate);

      const currentPreset = SCREEN_QUALITY_PRESETS[this._state.preset];
      if (!currentPreset) return;

      const targetBitrate = currentPreset.bitrateKbps;
      const threshold = targetBitrate * 0.6;

      if (this._currentEffectiveBitrate < threshold) {
        // Degrade to lower quality preset
        const presets = Object.keys(SCREEN_QUALITY_PRESETS);
        const currentIndex = presets.indexOf(this._state.preset);
        if (currentIndex > 0) {
          const lowerPreset = presets[currentIndex - 1];
          console.log(
            `[ScreenShare] Adaptive degradation: ${this._state.preset} → ${lowerPreset} (bitrate: ${this._currentEffectiveBitrate}Kbps < ${threshold}Kbps)`
          );
          await this.setQuality(lowerPreset);
        }
      }
    } catch (e) {
      console.warn('[ScreenShare] Bitrate monitoring error:', e);
    }
  }

  // ── Screen Annotation Readiness ─────────────────────

  /**
   * Get the active screen track for canvas overlay annotation.
   */
  getScreenTrack(): MediaStreamTrack | null {
    return this._state.stream?.getVideoTracks()[0] || null;
  }

  /**
   * Replace screen track with annotated version from canvas overlay.
   * Uses RTCRtpSender.replaceTrack() to swap tracks without renegotiation.
   */
  async replaceScreenTrackWithAnnotated(annotatedTrack: MediaStreamTrack): Promise<void> {
    const replacePromises: Promise<void>[] = [];

    for (const [peerId, sender] of this._screenSenders) {
      replacePromises.push(
        sender.replaceTrack(annotatedTrack).catch((e) => {
          console.warn(`[ScreenShare] Failed to replace with annotated track for ${peerId}:`, e);
        })
      );
    }

    await Promise.allSettled(replacePromises);
    console.log('[ScreenShare] Screen track replaced with annotated version');
  }

  // ── Retry Logic on Permission Denial ────────────────

  /**
   * Start screen sharing with automatic retry on permission denial.
   * Retries up to maxRetries times (default 3) with 1 second delay.
   */
  async startWithRetry(
    source: ScreenShareSource,
    mode: ScreenShareMode,
    preset: string,
    localStream: MediaStream,
    maxRetries: number = 3,
  ): Promise<MediaStream> {
    let lastError: any;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        return await this.start(source, mode, preset, localStream);
      } catch (e: any) {
        lastError = e;
        const msg = e?.message || String(e);

        if (!msg.includes('NotAllowedError') && !msg.includes('Permission denied')) {
          // Not a permission error, don't retry
          throw e;
        }

        if (attempt < maxRetries) {
          console.log(
            `[ScreenShare] Permission denied, retrying in 1s (attempt ${attempt}/${maxRetries})`
          );
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
      }
    }

    throw new Error(
      `Screen sharing permission denied after ${maxRetries} retries: ${lastError?.message}`
    );
  }

  // ── Source Validation ───────────────────────────────

  /**
   * Validate that a source ID still exists and is accessible.
   */
  async validateSource(sourceId: string): Promise<boolean> {
    try {
      const sources = await this.getAvailableSources();
      return sources.some((s) => s.id === sourceId);
    } catch {
      return false;
    }
  }

  /**
   * Fetch all currently available desktop sources from Electron.
   */
  async getAvailableSources(): Promise<ScreenShareSource[]> {
    try {
      const sources = await (navigator.mediaDevices as any).enumerateDisplayMedia?.() || [];
      return sources.map((s: any) => ({
        id: s.id || s.deviceId,
        name: s.label || s.name || 'Unknown',
        type: s.type === 'screen' ? 'screen' : s.type === 'window' ? 'window' : 'tab',
      }));
    } catch (e) {
      console.warn('[ScreenShare] Failed to enumerate display media:', e);
      return [];
    }
  }

  // ── Bandwidth Estimation ───────────────────────────

  /**
   * Estimate required bandwidth (Kbps) for screen sharing at given resolution and fps.
   * Uses simple heuristic: (width * height * fps) / quality_factor
   */
  estimateRequiredBandwidth(
    resolution: { width: number; height: number },
    fps: number,
  ): number {
    // Quality factor: higher = more compression = lower bitrate
    // Ranges from 80 (4K high-quality) to 400 (low-quality)
    const pixelCount = resolution.width * resolution.height;
    const qualityFactor = pixelCount >= 3840 * 2160 ? 80 : pixelCount >= 1920 * 1080 ? 120 : 200;

    const estimatedKbps = (pixelCount * fps) / qualityFactor;
    return Math.round(estimatedKbps);
  }

  /**
   * Get recommended quality preset based on available bandwidth.
   */
  getRecommendedPreset(availableBandwidthKbps: number): string {
    // Find the highest-quality preset that fits within available bandwidth
    const presetEntries = Object.entries(SCREEN_QUALITY_PRESETS)
      .sort((a, b) => b[1].bitrateKbps - a[1].bitrateKbps);

    for (const [name, preset] of presetEntries) {
      if (preset.bitrateKbps <= availableBandwidthKbps) {
        return name;
      }
    }

    // Fallback to lowest quality
    return '720p';
  }

  // ── Metrics ────────────────────────────────────────

  /**
   * Get current screen share metrics.
   */
  getMetrics(): ScreenShareMetrics {
    if (this._metricsStartTime !== null) {
      this._metricsData.duration = Date.now() - this._metricsStartTime;

      // Calculate average bitrate
      if (this._metricsSamples.length > 0) {
        this._metricsData.avgBitrate =
          Math.round(
            this._metricsSamples.reduce((a, b) => a + b, 0) / this._metricsSamples.length
          );
        this._metricsData.peakBitrate = Math.max(...this._metricsSamples);
      }
    }

    return { ...this._metricsData };
  }

  // ── Private: Track Routing ────────────────────────

  private async _startDualTrack(
    screenTrack: MediaStreamTrack,
    screenStream: MediaStream,
    qualityPreset: ScreenShareQualityPreset,
  ): Promise<void> {
    // Add screen as a separate track alongside camera

    if (this._peerConnection && !this._peerConnection.destroyed) {
      const sender = this._peerConnection.addScreenTrack(screenTrack, screenStream);
      this._screenSenders.set('single', sender);
      await this._applyBitrateToSender(sender, qualityPreset);
    }

    if (this._groupManager && !this._groupManager.destroyed) {
      const senders = this._groupManager.addScreenTrackAll(screenTrack, screenStream);
      for (const [peerId, sender] of senders) {
        this._screenSenders.set(peerId, sender);
        await this._applyBitrateToSender(sender, qualityPreset);
      }
    }
  }

  private async _startReplaceCamera(
    screenTrack: MediaStreamTrack,
    screenStream: MediaStream,
    localStream: MediaStream | null | undefined,
    qualityPreset: ScreenShareQualityPreset,
  ): Promise<void> {
    // Save current camera track for restoration
    if (localStream) {
      const cameraTrack = localStream.getVideoTracks()[0];
      if (cameraTrack) {
        this._savedCameraTrack = cameraTrack.clone();
      }
    }

    // Replace the camera track with screen track via replaceTrack
    // This avoids renegotiation — smoother transition
    const replacePromises: Promise<void>[] = [];

    if (this._peerConnection && !this._peerConnection.destroyed) {
      replacePromises.push(
        this._peerConnection.replaceTrack(screenTrack).then(() => {
          // Store reference for later removal/restoration
          // In replace mode, we use the existing video sender
        })
      );
    }

    if (this._groupManager && !this._groupManager.destroyed) {
      replacePromises.push(
        this._groupManager.replaceTrackAll(screenTrack)
      );
    }

    await Promise.allSettled(replacePromises);

    // Apply quality settings
    // In replace mode, the screen uses the existing video sender
    if (this._peerConnection && !this._peerConnection.destroyed) {
      const senders = (this._peerConnection as any).pc?.getSenders?.() || [];
      for (const sender of senders) {
        if (sender.track?.kind === 'video') {
          await this._applyBitrateToSender(sender, qualityPreset);
        }
      }
    }
  }

  private async _restoreCameraTrack(): Promise<void> {
    if (!this._savedCameraTrack) return;

    const restorePromises: Promise<void>[] = [];

    if (this._peerConnection && !this._peerConnection.destroyed) {
      restorePromises.push(
        this._peerConnection.replaceTrack(this._savedCameraTrack)
      );
    }

    if (this._groupManager && !this._groupManager.destroyed) {
      restorePromises.push(
        this._groupManager.replaceTrackAll(this._savedCameraTrack)
      );
    }

    await Promise.allSettled(restorePromises);
    console.log('[ScreenShare] Camera track restored');
  }

  // ── Private: Stream Acquisition ───────────────────

  private async _acquireScreenStream(
    sourceId: string,
    preset: ScreenShareQualityPreset,
  ): Promise<MediaStream> {
    // Electron desktop capture via chromeMediaSource
    return (navigator.mediaDevices as any).getUserMedia({
      audio: false,
      video: {
        mandatory: {
          chromeMediaSource: 'desktop',
          chromeMediaSourceId: sourceId,
          maxWidth: preset.maxWidth,
          maxHeight: preset.maxHeight,
          maxFrameRate: preset.maxFrameRate,
        },
      },
    });
  }

  private async _acquireScreenStreamWithAudio(
    sourceId: string,
    preset: ScreenShareQualityPreset,
  ): Promise<MediaStream> {
    // Electron desktop capture with system audio via chromeMediaSource
    return (navigator.mediaDevices as any).getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: 'desktop',
          chromeMediaSourceId: sourceId,
        },
      },
      video: {
        mandatory: {
          chromeMediaSource: 'desktop',
          chromeMediaSourceId: sourceId,
          maxWidth: preset.maxWidth,
          maxHeight: preset.maxHeight,
          maxFrameRate: preset.maxFrameRate,
        },
      },
    });
  }

  private async _applyBitrateToSender(
    sender: RTCRtpSender,
    preset: ScreenShareQualityPreset,
  ): Promise<void> {
    try {
      const params = sender.getParameters();
      if (!params.encodings || params.encodings.length === 0) {
        params.encodings = [{}];
      }
      params.encodings[0].maxBitrate = preset.bitrateKbps * 1000;
      params.encodings[0].maxFramerate = preset.maxFrameRate;
      await sender.setParameters(params);
    } catch {
      // Some browsers/versions don't support setParameters on all senders
    }
  }

  // ── Private: Error Handling ───────────────────────

  private _handleTrackEnded(): void {
    console.log('[ScreenShare] Track ended — cleaning up');
    this.callbacks.onSourceEnded();
    this.stop();
  }

  private _classifyError(e: any): string {
    const msg = e?.message || e?.name || String(e);

    if (msg.includes('Permission denied') || msg.includes('NotAllowedError')) {
      return 'Screen sharing permission denied. Please allow screen capture when prompted.';
    }
    if (msg.includes('NotFoundError') || msg.includes('Could not start')) {
      return 'Selected screen source is no longer available. The window may have been closed.';
    }
    if (msg.includes('NotReadableError')) {
      return 'Unable to read screen content. Another application may be blocking capture.';
    }
    if (msg.includes('AbortError')) {
      return 'Screen capture was aborted. Please try again.';
    }
    if (msg.includes('OverconstrainedError')) {
      return 'Selected quality settings are not supported. Try a lower quality preset.';
    }
    return `Screen sharing failed: ${msg}`;
  }

  // ── Private: State ────────────────────────────────

  private _updateState(partial: Partial<ScreenShareState>): void {
    this._state = { ...this._state, ...partial };
    this.callbacks.onStateChange({ ...this._state });
  }

  // ── Private: Metrics Initialization ────────────────

  private _initializeMetrics(screenTrack: MediaStreamTrack): void {
    this._metricsStartTime = Date.now();
    this._metricsSamples = [];
    this._metricsData = {
      duration: 0,
      sourceSwitches: 0,
      qualityChanges: 0,
      pauseCount: 0,
      avgBitrate: 0,
      peakBitrate: 0,
      framesDropped: 0,
      resolution: {
        width: (screenTrack.getSettings?.()?.width) || 0,
        height: (screenTrack.getSettings?.()?.height) || 0,
      },
    };
  }

  // ── Cleanup ───────────────────────────────────────

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    // Stop sharing if active
    if (this._state.stream) {
      this._state.stream.getTracks().forEach((t) => t.stop());
    }

    // Stop adaptive quality monitoring
    if (this._adaptiveQualityTimer) {
      clearInterval(this._adaptiveQualityTimer);
      this._adaptiveQualityTimer = null;
    }

    this._screenSenders.clear();
    this._savedCameraTrack = null;
    this._peerConnection = null;
    this._groupManager = null;
    this._metricsStartTime = null;
    this._metricsSamples = [];

    // Clean up metrics data
    this._metricsStartTime = 0;
    this._metricsSamples = [];
  }
}
