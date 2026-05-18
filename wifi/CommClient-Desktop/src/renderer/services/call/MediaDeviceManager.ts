/**
 * MediaDeviceManager — enumerate, select, and hot-swap audio/video devices.
 * Handles device change events (plug/unplug), and maintains current selections.
 * Manages local MediaStream lifecycle.
 *
 * getUserMedia constraints are sourced from mediaConstraintsBuilder so the
 * Settings UI, the server's media policy, and autoMaxQuality probing apply to
 * real calls (not just the Settings preview).
 */
import { buildCallConstraints } from './mediaConstraintsBuilder';
import { getVirtualSource, listVirtualSources } from '@/stores/virtualSources.store';
import { useSettingsStore } from '@/stores/settings.store';

export interface DeviceInfo {
  deviceId: string;
  label: string;
  kind: 'audioinput' | 'audiooutput' | 'videoinput';
  groupId: string;
}

export interface DeviceSelection {
  audioInput: string;   // deviceId or 'default'
  audioOutput: string;  // deviceId or 'default'
  videoInput: string;   // deviceId or '' (none)
}

export interface MediaConstraintOptions {
  audio: boolean;
  video: boolean;
  audioDeviceId?: string;
  videoDeviceId?: string;
  videoWidth?: number;
  videoHeight?: number;
  videoFrameRate?: number;
}

export interface AudioMonitorHandle {
  getLevel: () => number;          // 0-1 smoothed RMS
  getPeak: () => number;           // 0-1 peak
  isSpeaking: () => boolean;       // above threshold
  isSilent: () => boolean;         // below silence threshold for >2s
  stop: () => void;
}

type DeviceChangeCallback = (devices: DeviceInfo[]) => void;
type PermissionState = 'granted' | 'denied' | 'prompt';

// Virtual sources are streams that did NOT come from navigator.mediaDevices
// (e.g. a paired phone camera streamed over WebRTC). They are exposed as
// selectable devices alongside real hardware. deviceIds are prefixed with
// "virtual:" so acquireLocalStream() can route around getUserMedia().
export const VIRTUAL_DEVICE_PREFIX = 'virtual:';
export function isVirtualDeviceId(id: string | undefined | null): boolean {
  return !!id && id.startsWith(VIRTUAL_DEVICE_PREFIX);
}

export class MediaDeviceManager {
  private _devices: DeviceInfo[] = [];
  private _selection: DeviceSelection = {
    audioInput: 'default',
    audioOutput: 'default',
    videoInput: '',
  };
  private _localStream: MediaStream | null = null;
  private _screenStream: MediaStream | null = null;
  private _listeners: DeviceChangeCallback[] = [];
  private _boundHandleChange: () => void;

  // Permission state tracking
  private _permissionCache: Map<string, PermissionState> = new Map();

  // Noise suppression & audio processing flags
  private _noiseSuppression: boolean = true;
  private _echoCancellation: boolean = true;
  private _autoGainControl: boolean = true;

  constructor() {
    this._boundHandleChange = this._handleDeviceChange.bind(this);
  }

  // ── Initialization ────────────────────────────────

  /**
   * Start listening for device changes and enumerate initial devices.
   */
  async init(): Promise<void> {
    navigator.mediaDevices.addEventListener('devicechange', this._boundHandleChange);
    await this.enumerateDevices();
  }

  /**
   * Stop listening for device changes.
   */
  destroy(): void {
    navigator.mediaDevices.removeEventListener('devicechange', this._boundHandleChange);
    this.releaseLocalStream();
    this.releaseScreenStream();
    this._listeners = [];
  }

  // ── Device Enumeration ────────────────────────────

  async enumerateDevices(): Promise<DeviceInfo[]> {
    // Request temporary permissions to get device labels
    let tempStream: MediaStream | null = null;
    try {
      const rawDevices = await navigator.mediaDevices.enumerateDevices();
      const hasLabels = rawDevices.some((d) => d.label);

      if (!hasLabels) {
        // Need temporary stream to get labels
        tempStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true }).catch(() =>
          navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null)
        );
      }

      const devices = await navigator.mediaDevices.enumerateDevices();
      this._devices = devices
        .filter((d) => (d.deviceId && d.kind !== 'audiooutput') || d.kind === 'audiooutput')
        .map((d) => ({
          deviceId: d.deviceId,
          label: d.label || this._fallbackLabel(d.kind as DeviceInfo['kind'], d.deviceId),
          kind: d.kind as DeviceInfo['kind'],
          groupId: d.groupId,
        }));
    } finally {
      tempStream?.getTracks().forEach((t) => t.stop());
    }

    return this._devices;
  }

  private _fallbackLabel(kind: DeviceInfo['kind'], id: string): string {
    const prefix = kind === 'audioinput' ? 'Microphone' : kind === 'audiooutput' ? 'Speaker' : 'Camera';
    return `${prefix} (${id.slice(0, 8)})`;
  }

  get devices(): DeviceInfo[] {
    const virtuals: DeviceInfo[] = listVirtualSources().map((v) => ({
      deviceId: v.deviceId, label: v.label, kind: v.kind, groupId: v.deviceId,
    }));
    return [...this._devices, ...virtuals];
  }

  getDevicesByKind(kind: DeviceInfo['kind']): DeviceInfo[] {
    const virtuals: DeviceInfo[] = (kind === 'audiooutput' ? [] : listVirtualSources(kind as 'audioinput' | 'videoinput')).map((v) => ({
      deviceId: v.deviceId, label: v.label, kind: v.kind, groupId: v.deviceId,
    }));
    return [...this._devices.filter((d) => d.kind === kind), ...virtuals];
  }

  /**
   * Get device by ID from the enumerated devices list (real or virtual).
   */
  getDeviceById(deviceId: string): DeviceInfo | undefined {
    const virt = getVirtualSource(deviceId);
    if (virt) return { deviceId: virt.deviceId, label: virt.label, kind: virt.kind, groupId: virt.deviceId };
    return this._devices.find((d) => d.deviceId === deviceId);
  }

  getVirtualStream(deviceId: string): MediaStream | null {
    return getVirtualSource(deviceId)?.stream || null;
  }

  /**
   * Check if a device still exists in the enumerated devices list. Virtual
   * sources (e.g. paired-phone streams) are also considered.
   */
  async isDeviceAvailable(deviceId: string): Promise<boolean> {
    if (isVirtualDeviceId(deviceId)) {
      return !!getVirtualSource(deviceId);
    }
    await this.enumerateDevices();
    return !!this._devices.find((d) => d.deviceId === deviceId);
  }

  // ── Device Selection ──────────────────────────────

  get selection(): DeviceSelection {
    return { ...this._selection };
  }

  setAudioInput(deviceId: string): void {
    this._selection.audioInput = deviceId;
  }

  setAudioOutput(deviceId: string): void {
    this._selection.audioOutput = deviceId;
  }

  setVideoInput(deviceId: string): void {
    this._selection.videoInput = deviceId;
  }

  /**
   * Set the audio output device on an HTMLAudioElement or HTMLVideoElement.
   * (Only works in Chromium-based browsers)
   */
  async setOutputDevice(element: HTMLMediaElement): Promise<void> {
    if ('setSinkId' in element && this._selection.audioOutput) {
      try {
        await (element as any).setSinkId(this._selection.audioOutput);
      } catch (e) {
        console.warn('[MediaDevice] setSinkId failed:', e);
      }
    }
  }

  // ── Permission Management ─────────────────────────

  /**
   * Get permission state for camera or microphone.
   * Returns 'granted', 'denied', or 'prompt'.
   */
  async getPermissionState(kind: 'camera' | 'microphone'): Promise<PermissionState> {
    const permissionName = kind === 'camera' ? 'camera' : 'microphone';
    const cacheKey = `perm_${permissionName}`;

    // Check cache first
    if (this._permissionCache.has(cacheKey)) {
      return this._permissionCache.get(cacheKey) as PermissionState;
    }

    // Query actual permission state if available
    if ('permissions' in navigator) {
      try {
        const result = await (navigator.permissions as any).query({ name: permissionName });
        const state: PermissionState = result.state;
        this._permissionCache.set(cacheKey, state);
        return state;
      } catch (e) {
        console.warn(`[MediaDevice] Failed to query ${permissionName} permission:`, e);
      }
    }

    // Default to 'prompt' if query unavailable
    return 'prompt';
  }

  /**
   * Request permissions for audio and/or video.
   * Returns actual granted permissions.
   */
  async requestPermissions(audio: boolean, video: boolean): Promise<{ audio: boolean; video: boolean }> {
    const result = { audio: false, video: false };

    try {
      const constraints: MediaStreamConstraints = {
        audio: audio ? { echoCancellation: this._echoCancellation, noiseSuppression: this._noiseSuppression, autoGainControl: this._autoGainControl } : false,
        video: video ? { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } } : false,
      };

      const stream = await navigator.mediaDevices.getUserMedia(constraints);

      if (audio) {
        result.audio = stream.getAudioTracks().length > 0;
        this._permissionCache.set('perm_microphone', 'granted');
      }
      if (video) {
        result.video = stream.getVideoTracks().length > 0;
        this._permissionCache.set('perm_camera', 'granted');
      }

      // Clean up the temporary stream
      stream.getTracks().forEach((t) => t.stop());
    } catch (e) {
      console.warn('[MediaDevice] requestPermissions failed:', e);
      this._permissionCache.set('perm_microphone', 'denied');
      this._permissionCache.set('perm_camera', 'denied');
    }

    return result;
  }

  // ── Local Stream ──────────────────────────────────

  /**
   * Acquire a local media stream with the given constraints.
   * Releases any existing stream first.
   */
  async acquireLocalStream(opts: MediaConstraintOptions): Promise<MediaStream> {
    this.releaseLocalStream();

    // Fall back to the settings store when neither the explicit opts nor the
    // internal _selection carry a device. The Settings UI writes directly to
    // the store; without this bridge, acquireLocalStream would ignore the
    // user's picked camera (including any virtual source like a paired phone
    // or USB iPhone) and the virtual shortcut below would never fire.
    const storeSettings = useSettingsStore.getState().settings;
    const audioId =
      opts.audioDeviceId ||
      (this._selection.audioInput && this._selection.audioInput !== 'default'
        ? this._selection.audioInput
        : storeSettings.audioInputDevice) ||
      'default';
    const videoId =
      opts.videoDeviceId ||
      this._selection.videoInput ||
      storeSettings.videoInputDevice ||
      '';
    const virtAudio = opts.audio && isVirtualDeviceId(audioId) ? this.getVirtualStream(audioId) : null;
    const virtVideo = opts.video && isVirtualDeviceId(videoId) ? this.getVirtualStream(videoId) : null;

    // Virtual-only shortcut: if every requested kind is served by a virtual
    // source, skip getUserMedia entirely. Combine tracks from potentially
    // distinct virtual streams into a single MediaStream for the caller.
    const audioCoveredByVirtual = !opts.audio || virtAudio;
    const videoCoveredByVirtual = !opts.video || virtVideo;
    if (audioCoveredByVirtual && videoCoveredByVirtual && (virtAudio || virtVideo)) {
      const combined = new MediaStream();
      if (virtAudio) virtAudio.getAudioTracks().forEach((t) => combined.addTrack(t));
      if (virtVideo) virtVideo.getVideoTracks().forEach((t) => combined.addTrack(t));
      this._localStream = combined;
      return combined;
    }

    const built = await buildCallConstraints({
      audio: opts.audio && !virtAudio,
      video: opts.video && !virtVideo,
      audioDeviceId: virtAudio ? undefined : audioId,
      videoDeviceId: virtVideo ? undefined : videoId,
    });

    if (opts.video && built.video) {
      if (opts.videoWidth)     built.video.width     = { ideal: opts.videoWidth };
      if (opts.videoHeight)    built.video.height    = { ideal: opts.videoHeight };
      if (opts.videoFrameRate) built.video.frameRate = { ideal: opts.videoFrameRate };
    }

    // Defense-in-depth (audit fix 2.5): on a device with broken /
    // missing camera, the combined audio+video getUserMedia fails
    // even when the caller asked for audio AND would happily accept
    // a downgrade. Try the combined request first; if it throws and
    // audio is also requested, retry audio-only so the call still
    // starts with at least one working track. The caller can then
    // detect the missing video track via stream.getVideoTracks().
    let hardware: MediaStream;
    try {
      hardware = await navigator.mediaDevices.getUserMedia({
        audio: built.audio,
        video: built.video,
      });
    } catch (err) {
      const wantedVideo = !!built.video;
      const wantedAudio = !!built.audio;
      if (wantedVideo && wantedAudio) {
        console.warn(
          '[MediaDeviceManager] combined getUserMedia failed, retrying audio-only:',
          (err as Error).message,
        );
        hardware = await navigator.mediaDevices.getUserMedia({
          audio: built.audio,
          video: false,
        });
      } else {
        throw err;
      }
    }

    // Mix virtual + hardware tracks into one MediaStream.
    if (virtAudio || virtVideo) {
      const combined = new MediaStream();
      hardware.getTracks().forEach((t) => combined.addTrack(t));
      if (virtAudio) virtAudio.getAudioTracks().forEach((t) => combined.addTrack(t));
      if (virtVideo) virtVideo.getVideoTracks().forEach((t) => combined.addTrack(t));
      this._localStream = combined;
      return combined;
    }

    this._localStream = hardware;
    return hardware;
  }

  get localStream(): MediaStream | null {
    return this._localStream;
  }

  releaseLocalStream(): void {
    if (this._localStream) {
      // Collect tracks that belong to a virtual source — they are owned by
      // the upstream bridge (e.g. PhonePairBridge), so we must NOT stop
      // them; doing so would kill the phone's pipeline for everyone.
      const virtualTracks = new Set<MediaStreamTrack>();
      for (const src of listVirtualSources()) {
        for (const t of src.stream.getTracks()) virtualTracks.add(t);
      }
      this._localStream.getTracks().forEach((t) => {
        if (!virtualTracks.has(t)) {
          t.stop();
          t.enabled = false;
        }
      });
      this._localStream = null;
    }
  }

  // ── Track Controls ────────────────────────────────

  muteAudio(muted: boolean): void {
    this._localStream?.getAudioTracks().forEach((t) => {
      t.enabled = !muted;
    });
  }

  muteVideo(off: boolean): void {
    this._localStream?.getVideoTracks().forEach((t) => {
      t.enabled = !off;
    });
  }

  get isAudioMuted(): boolean {
    const track = this._localStream?.getAudioTracks()[0];
    return track ? !track.enabled : true;
  }

  get isVideoOff(): boolean {
    const track = this._localStream?.getVideoTracks()[0];
    return track ? !track.enabled : true;
  }

  /** Remove a track from the local stream and stop it only if it isn't owned
   *  by a virtual source (the source pipeline must keep the track alive). */
  private _swapOutTrack(oldTrack: MediaStreamTrack | null | undefined): void {
    if (!oldTrack || !this._localStream) return;
    this._localStream.removeTrack(oldTrack);
    const owned = listVirtualSources().some((s) =>
      s.stream.getTracks().includes(oldTrack),
    );
    if (!owned) oldTrack.stop();
  }

  /**
   * Hot-swap the audio input device without tearing down the peer connection.
   * Replaces the audio track in the local stream. Virtual deviceIds (paired
   * phone, etc.) bypass getUserMedia and reuse the bridge's existing track.
   */
  async switchAudioInput(deviceId: string): Promise<MediaStreamTrack | null> {
    const isAvailable = await this.isDeviceAvailable(deviceId);
    if (!isAvailable) {
      console.warn(`[MediaDevice] Audio device ${deviceId} not available`);
      return null;
    }

    this._selection.audioInput = deviceId;

    if (!this._localStream) return null;

    const oldTrack = this._localStream.getAudioTracks()[0];
    const wasMuted = oldTrack ? !oldTrack.enabled : false;

    let newTrack: MediaStreamTrack | null = null;
    if (isVirtualDeviceId(deviceId)) {
      const virt = this.getVirtualStream(deviceId);
      newTrack = virt?.getAudioTracks()[0] || null;
    } else {
      const built = await buildCallConstraints({
        audio: true,
        video: false,
        audioDeviceId: deviceId,
      });
      const newStream = await navigator.mediaDevices.getUserMedia({ audio: built.audio });
      newTrack = newStream.getAudioTracks()[0];
    }

    if (!newTrack) {
      console.warn(`[MediaDevice] No audio track from device ${deviceId}`);
      return null;
    }
    newTrack.enabled = !wasMuted;

    this._swapOutTrack(oldTrack);
    this._localStream.addTrack(newTrack);

    return newTrack;
  }

  /**
   * Hot-swap the video input device. Virtual deviceIds (paired phone, etc.)
   * reuse the existing bridge track without touching getUserMedia.
   */
  async switchVideoInput(deviceId: string): Promise<MediaStreamTrack | null> {
    const isAvailable = await this.isDeviceAvailable(deviceId);
    if (!isAvailable) {
      console.warn(`[MediaDevice] Video device ${deviceId} not available`);
      return null;
    }

    this._selection.videoInput = deviceId;

    if (!this._localStream) return null;

    const oldTrack = this._localStream.getVideoTracks()[0];
    const wasOff = oldTrack ? !oldTrack.enabled : false;

    let newTrack: MediaStreamTrack | null = null;
    if (isVirtualDeviceId(deviceId)) {
      const virt = this.getVirtualStream(deviceId);
      newTrack = virt?.getVideoTracks()[0] || null;
    } else {
      const built = await buildCallConstraints({
        audio: false,
        video: true,
        videoDeviceId: deviceId,
      });
      const newStream = await navigator.mediaDevices.getUserMedia({ video: built.video });
      newTrack = newStream.getVideoTracks()[0];
    }

    if (!newTrack) {
      console.warn(`[MediaDevice] No video track from device ${deviceId}`);
      return null;
    }
    newTrack.enabled = !wasOff;

    this._swapOutTrack(oldTrack);
    this._localStream.addTrack(newTrack);

    return newTrack;
  }

  // ── Screen Capture ────────────────────────────────

  /**
   * Capture a screen/window source using Electron's desktopCapturer.
   */
  async acquireScreenStream(sourceId: string): Promise<MediaStream> {
    this.releaseScreenStream();

    // Electron-specific: chromeMediaSource
    this._screenStream = await (navigator.mediaDevices as any).getUserMedia({
      audio: false,
      video: {
        mandatory: {
          chromeMediaSource: 'desktop',
          chromeMediaSourceId: sourceId,
          maxWidth: 1920,
          maxHeight: 1080,
          maxFrameRate: 30,
        },
      },
    });

    return this._screenStream!;
  }

  get screenStream(): MediaStream | null {
    return this._screenStream;
  }

  releaseScreenStream(): void {
    if (this._screenStream) {
      this._screenStream.getTracks().forEach((t) => t.stop());
      this._screenStream = null;
    }
  }

  // ── Audio Processing (Noise Suppression, Echo Cancellation, AGC) ────────

  /**
   * Enable or disable noise suppression on audio tracks.
   * Applies constraint to existing local stream audio tracks if available.
   */
  async enableNoiseSuppression(enabled: boolean): Promise<void> {
    this._noiseSuppression = enabled;

    if (this._localStream) {
      const audioTracks = this._localStream.getAudioTracks();
      for (const track of audioTracks) {
        try {
          await track.applyConstraints({ noiseSuppression: enabled });
        } catch (e) {
          console.warn('[MediaDevice] Failed to apply noiseSuppression constraint:', e);
        }
      }
    }
  }

  /**
   * Enable or disable echo cancellation on audio tracks.
   */
  async enableEchoCancellation(enabled: boolean): Promise<void> {
    this._echoCancellation = enabled;

    if (this._localStream) {
      const audioTracks = this._localStream.getAudioTracks();
      for (const track of audioTracks) {
        try {
          await track.applyConstraints({ echoCancellation: enabled });
        } catch (e) {
          console.warn('[MediaDevice] Failed to apply echoCancellation constraint:', e);
        }
      }
    }
  }

  /**
   * Enable or disable automatic gain control on audio tracks.
   */
  async enableAutoGainControl(enabled: boolean): Promise<void> {
    this._autoGainControl = enabled;

    if (this._localStream) {
      const audioTracks = this._localStream.getAudioTracks();
      for (const track of audioTracks) {
        try {
          await track.applyConstraints({ autoGainControl: enabled });
        } catch (e) {
          console.warn('[MediaDevice] Failed to apply autoGainControl constraint:', e);
        }
      }
    }
  }

  // ── Audio Level Monitoring ────────────────────────

  /**
   * Get current audio input level (0-1). Useful for voice activity detection.
   */
  createAudioLevelMonitor(stream: MediaStream): { getLevel: () => number; stop: () => void } {
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);

    const dataArray = new Uint8Array(analyser.frequencyBinCount);

    return {
      getLevel: (): number => {
        analyser.getByteFrequencyData(dataArray);
        const sum = dataArray.reduce((a, b) => a + b, 0);
        return sum / (dataArray.length * 255);
      },
      stop: () => {
        source.disconnect();
        ctx.close().catch(() => {});
      },
    };
  }

  /**
   * Create an advanced audio monitor with smoothed RMS, peak detection, and speaking/silence detection.
   * Includes configurable thresholds for voice activity detection.
   */
  createAdvancedAudioMonitor(stream: MediaStream): AudioMonitorHandle {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    const dataArray = new Uint8Array(analyser.frequencyBinCount);
    let smoothedLevel = 0;
    let peakLevel = 0;
    let silenceDuration = 0;
    let lastUpdateTime = Date.now();

    const SMOOTHING_FACTOR = 0.7;
    const SPEAKING_THRESHOLD = 0.05;
    const SILENCE_THRESHOLD = 0.02;
    const SILENCE_DURATION_THRESHOLD = 2000; // 2 seconds

    const calculateRMS = (): number => {
      analyser.getByteFrequencyData(dataArray);
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        const normalized = dataArray[i] / 255;
        sum += normalized * normalized;
      }
      return Math.sqrt(sum / dataArray.length);
    };

    const monitor = {
      getLevel: (): number => {
        const currentRMS = calculateRMS();
        smoothedLevel = smoothedLevel * SMOOTHING_FACTOR + currentRMS * (1 - SMOOTHING_FACTOR);
        return Math.min(1, smoothedLevel);
      },

      getPeak: (): number => {
        const currentRMS = calculateRMS();
        peakLevel = Math.max(peakLevel * 0.99, currentRMS); // Decay peak over time
        return Math.min(1, peakLevel);
      },

      isSpeaking: (): boolean => {
        const level = monitor.getLevel();
        return level > SPEAKING_THRESHOLD;
      },

      isSilent: (): boolean => {
        const level = monitor.getLevel();
        const now = Date.now();
        const timeDelta = now - lastUpdateTime;
        lastUpdateTime = now;

        if (level < SILENCE_THRESHOLD) {
          silenceDuration += timeDelta;
        } else {
          silenceDuration = 0;
        }

        return silenceDuration > SILENCE_DURATION_THRESHOLD;
      },

      stop: (): void => {
        source.disconnect();
        analyser.disconnect();
        ctx.close().catch(() => {});
      },
    };

    return monitor;
  }

  // ── Video Resolution & Constraints ────────────────

  /**
   * Set video resolution and frame rate for the active video track.
   * Applies constraints to the existing video track if available.
   */
  async setVideoResolution(width: number, height: number, frameRate?: number): Promise<void> {
    if (!this._localStream) {
      console.warn('[MediaDevice] No local stream available for video resolution change');
      return;
    }

    const videoTracks = this._localStream.getVideoTracks();
    if (videoTracks.length === 0) {
      console.warn('[MediaDevice] No video track in local stream');
      return;
    }

    const constraints: MediaTrackConstraints = {
      width: { ideal: width, max: width * 1.5 },
      height: { ideal: height, max: height * 1.5 },
    };

    if (frameRate) {
      constraints.frameRate = { ideal: frameRate, max: frameRate + 10 };
    }

    try {
      for (const track of videoTracks) {
        await track.applyConstraints(constraints);
      }
    } catch (e) {
      console.warn('[MediaDevice] Failed to set video resolution:', e);
    }
  }

  /**
   * Get capabilities of the active video track.
   */
  getVideoTrackCapabilities(): MediaTrackCapabilities | null {
    if (!this._localStream) return null;
    const videoTrack = this._localStream.getVideoTracks()[0];
    if (!videoTrack) return null;
    return videoTrack.getCapabilities?.() || null;
  }

  /**
   * Apply arbitrary constraints to the active video track.
   */
  async applyVideoConstraints(constraints: MediaTrackConstraints): Promise<void> {
    if (!this._localStream) {
      console.warn('[MediaDevice] No local stream available for video constraints');
      return;
    }

    const videoTracks = this._localStream.getVideoTracks();
    if (videoTracks.length === 0) {
      console.warn('[MediaDevice] No video track in local stream');
      return;
    }

    try {
      for (const track of videoTracks) {
        await track.applyConstraints(constraints);
      }
    } catch (e) {
      console.warn('[MediaDevice] Failed to apply video constraints:', e);
    }
  }

  // ── Media Stream Cloning & Mixing ────────────────

  /**
   * Clone the local stream, creating a new MediaStream with copied tracks.
   * Useful for sending to multiple peer connections or processors.
   */
  cloneLocalStream(): MediaStream | null {
    if (!this._localStream) return null;

    const clonedStream = new MediaStream();

    // Clone audio tracks
    this._localStream.getAudioTracks().forEach((track) => {
      clonedStream.addTrack(track.clone());
    });

    // Clone video tracks
    this._localStream.getVideoTracks().forEach((track) => {
      clonedStream.addTrack(track.clone());
    });

    return clonedStream;
  }

  /**
   * Create a mixed MediaStream from multiple streams.
   * Combines audio tracks from all input streams into a single output stream.
   * Note: Video tracks from the first stream with video are used.
   */
  createMixedStream(streams: MediaStream[]): MediaStream {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const destination = ctx.createMediaStreamDestination();

    const mixedStream = new MediaStream();

    // Mix audio from all streams
    streams.forEach((stream) => {
      const audioTracks = stream.getAudioTracks();
      audioTracks.forEach((track) => {
        try {
          const source = ctx.createMediaStreamSource(new MediaStream([track]));
          source.connect(destination);
        } catch (e) {
          console.warn('[MediaDevice] Failed to add audio track to mix:', e);
        }
      });
    });

    // Add mixed audio track to output
    if (destination.stream.getAudioTracks().length > 0) {
      mixedStream.addTrack(destination.stream.getAudioTracks()[0]);
    }

    // Optionally include video from first stream that has video
    for (const stream of streams) {
      const videoTracks = stream.getVideoTracks();
      if (videoTracks.length > 0) {
        mixedStream.addTrack(videoTracks[0].clone());
        break;
      }
    }

    return mixedStream;
  }

  // ── Device Preference Persistence ────────────────

  /**
   * Save the current device preferences to localStorage.
   */
  saveDevicePreferences(): void {
    const prefs = {
      audioInput: this._selection.audioInput,
      videoInput: this._selection.videoInput,
      audioOutput: this._selection.audioOutput,
    };
    try {
      localStorage.setItem('MediaDeviceManager_preferences', JSON.stringify(prefs));
    } catch (e) {
      console.warn('[MediaDevice] Failed to save device preferences:', e);
    }
  }

  /**
   * Load device preferences from localStorage.
   * Returns an object with audioInput, videoInput, and audioOutput device IDs (if previously saved).
   */
  loadDevicePreferences(): { audioInput?: string; videoInput?: string; audioOutput?: string } {
    try {
      const stored = localStorage.getItem('MediaDeviceManager_preferences');
      if (stored) {
        return JSON.parse(stored);
      }
    } catch (e) {
      console.warn('[MediaDevice] Failed to load device preferences:', e);
    }
    return {};
  }

  // ── Device Change Events ──────────────────────────

  onDeviceChange(cb: DeviceChangeCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter((l) => l !== cb);
    };
  }

  private async _handleDeviceChange(): Promise<void> {
    await this.enumerateDevices();
    for (const cb of this._listeners) {
      try {
        cb(this._devices);
      } catch (e) {
        console.error('[MediaDevice] listener error:', e);
      }
    }
  }
}
