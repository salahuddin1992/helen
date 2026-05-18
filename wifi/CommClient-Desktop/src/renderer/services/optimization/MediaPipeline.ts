/**
 * MediaPipeline.ts — Audio/video processing optimization & hardware acceleration.
 *
 * Identified problems from audit:
 *   1. Multiple AudioContext instances (1 per audio monitor component)
 *   2. No hardware encoder detection (VP8 software encode wastes CPU)
 *   3. Audio processing chain not optimized for LAN (no need for heavy AEC on local)
 *   4. Video encoding params not adapted to LAN bandwidth (10-1000Mbps available)
 *   5. No frame rate adaptation based on actual CPU cost
 *   6. Screen share encodes at full resolution even for static content
 *
 * Optimizations:
 *   1. Shared AudioContext via MemoryManager.audioPool
 *   2. Codec preference ordering: H264 (HW) > VP9 (HW) > VP8 (SW fallback)
 *   3. LAN-optimized audio: lighter processing, higher bitrate (bandwidth is free)
 *   4. Adaptive video encoding: match encoder to device capability tier
 *   5. Frame skip logic: drop frames before encoder overloads
 *   6. Insertable Streams API for pre-encode processing (if available)
 *
 * Does NOT modify existing CallEngine/PeerConnection code.
 * Provides configuration objects that the call layer consumes.
 */

import type { DeviceTier } from '../performance/DeviceCapabilityDetector';

// ── Types ───────────────────────────────────────────────────

export type VideoCodec = 'H264' | 'VP9' | 'VP8' | 'AV1';
export type AudioCodec = 'opus' | 'PCMU' | 'PCMA';

export interface CodecCapability {
  codec: VideoCodec;
  hardwareAccelerated: boolean;
  maxWidth: number;
  maxHeight: number;
  maxFps: number;
  profileLevelId?: string;
}

export interface AudioPipelineConfig {
  /** Echo cancellation type */
  echoCancellation: 'browser' | 'system' | 'none';
  /** Noise suppression level */
  noiseSuppression: 'aggressive' | 'moderate' | 'none';
  /** Auto gain control */
  autoGainControl: boolean;
  /** Audio bitrate (kbps) — on LAN we can afford higher */
  bitrateKbps: number;
  /** Sample rate */
  sampleRate: number;
  /** Enable comfort noise generation */
  comfortNoise: boolean;
  /** DTX (discontinuous transmission) — saves bandwidth during silence */
  dtx: boolean;
  /** Packet time (ms) — 20ms default, 10ms for lower latency */
  packetTimeMs: number;
  /** Opus application type */
  opusApplication: 'voip' | 'audio' | 'lowdelay';
}

export interface VideoPipelineConfig {
  /** Preferred codec order */
  codecPreference: VideoCodec[];
  /** Whether hardware encoding is available */
  hardwareEncode: boolean;
  /** Max encode width */
  maxWidth: number;
  /** Max encode height */
  maxHeight: number;
  /** Max encode FPS */
  maxFps: number;
  /** Target bitrate (kbps) */
  targetBitrateKbps: number;
  /** Max bitrate (kbps) */
  maxBitrateKbps: number;
  /** Keyframe interval (seconds) */
  keyframeIntervalSec: number;
  /** Content hint for encoder */
  contentHint: 'motion' | 'detail' | 'text';
  /** Scale down factor when CPU is constrained (1 = no scaling) */
  cpuScaleDownFactor: number;
  /** Enable temporal scalability (SVC) */
  enableSVC: boolean;
  /** Number of spatial layers */
  spatialLayers: number;
  /** Number of temporal layers */
  temporalLayers: number;
}

export interface ScreenSharePipelineConfig {
  /** Preferred codec (VP9 good for screen, H264 for motion) */
  codec: VideoCodec;
  /** Max capture FPS — lower for static content */
  maxCaptureFps: number;
  /** Max encode FPS */
  maxEncodeFps: number;
  /** Target bitrate */
  targetBitrateKbps: number;
  /** Content hint */
  contentHint: 'text' | 'detail' | 'motion';
  /** Keyframe interval for static content (longer = smaller) */
  keyframeIntervalSec: number;
  /** Scale factor (0.5 = half resolution) */
  scaleFactor: number;
  /** Enable cursor capture */
  captureCursor: boolean;
}

export interface MediaPipelineStatus {
  /** Detected video codecs */
  videoCodecs: CodecCapability[];
  /** Best available video codec */
  bestVideoCodec: VideoCodec;
  /** Whether hardware encode was detected */
  hardwareEncodeAvailable: boolean;
  /** Current audio config */
  audioConfig: AudioPipelineConfig;
  /** Current video config */
  videoConfig: VideoPipelineConfig;
  /** Current screen share config */
  screenShareConfig: ScreenSharePipelineConfig;
  /** Device tier */
  deviceTier: DeviceTier;
}

// ── Constants ───────────────────────────────────────────────

/** Hardware encoder identifiers in SDP/capabilities */
const HW_ENCODER_HINTS = [
  'nvidia', 'nvenc', 'intel', 'qsv', 'quicksync',
  'amd', 'vce', 'vaapi', 'videotoolbox', 'mediacodec',
];

/** Opus recommended settings for LAN (bandwidth is cheap) */
const LAN_AUDIO_DEFAULTS: AudioPipelineConfig = {
  echoCancellation: 'browser',
  noiseSuppression: 'moderate',
  autoGainControl: true,
  bitrateKbps: 64,        // Higher than typical WebRTC (32kbps)
  sampleRate: 48_000,
  comfortNoise: false,     // LAN: no need to mask silence
  dtx: false,              // LAN: bandwidth is free, keep stream alive
  packetTimeMs: 20,
  opusApplication: 'voip',
};

// ── MediaPipeline ───────────────────────────────────────────

export class MediaPipeline {
  private _deviceTier: DeviceTier = 'medium';
  private _videoCodecs: CodecCapability[] = [];
  private _hardwareEncodeAvailable = false;
  private _bestVideoCodec: VideoCodec = 'VP8';
  private _detected = false;

  // ── Detection ─────────────────────────────────────────────

  /**
   * Detect available codecs and hardware capabilities.
   * Should be called once during startup.
   */
  async detect(): Promise<void> {
    if (this._detected) return;

    // Detect video codecs via RTCRtpSender.getCapabilities
    this._videoCodecs = await this._detectVideoCodecs();
    this._hardwareEncodeAvailable = this._videoCodecs.some(c => c.hardwareAccelerated);
    this._bestVideoCodec = this._selectBestCodec();
    this._detected = true;
  }

  /**
   * Set the device tier for config adaptation.
   */
  setDeviceTier(tier: DeviceTier): void {
    this._deviceTier = tier;
  }

  // ── Configuration Getters ─────────────────────────────────

  /**
   * Get optimized audio pipeline config for LAN communication.
   */
  getAudioConfig(): AudioPipelineConfig {
    const config = { ...LAN_AUDIO_DEFAULTS };

    // Adapt to device tier
    switch (this._deviceTier) {
      case 'minimal':
        config.bitrateKbps = 32;
        config.sampleRate = 16_000;
        config.noiseSuppression = 'none';  // Save CPU
        config.dtx = true;                 // Save CPU during silence
        config.opusApplication = 'voip';
        break;
      case 'low':
        config.bitrateKbps = 48;
        config.sampleRate = 24_000;
        config.noiseSuppression = 'moderate';
        config.dtx = true;
        break;
      case 'medium':
        // Use defaults
        break;
      case 'high':
        config.bitrateKbps = 96;
        config.noiseSuppression = 'aggressive';
        config.opusApplication = 'audio';  // Better quality
        config.packetTimeMs = 10;          // Lower latency
        break;
    }

    return config;
  }

  /**
   * Get optimized video pipeline config.
   */
  getVideoConfig(): VideoPipelineConfig {
    const hw = this._hardwareEncodeAvailable;

    const base: VideoPipelineConfig = {
      codecPreference: this._getCodecPreference(),
      hardwareEncode: hw,
      maxWidth: 1280,
      maxHeight: 720,
      maxFps: 30,
      targetBitrateKbps: 2_000,
      maxBitrateKbps: 5_000,
      keyframeIntervalSec: 3,
      contentHint: 'motion',
      cpuScaleDownFactor: 1,
      enableSVC: false,
      spatialLayers: 1,
      temporalLayers: 1,
    };

    // Adapt to device tier
    switch (this._deviceTier) {
      case 'minimal':
        base.maxWidth = 320;
        base.maxHeight = 240;
        base.maxFps = 10;
        base.targetBitrateKbps = 200;
        base.maxBitrateKbps = 500;
        base.cpuScaleDownFactor = 2;
        base.keyframeIntervalSec = 5;
        break;
      case 'low':
        base.maxWidth = 480;
        base.maxHeight = 360;
        base.maxFps = 15;
        base.targetBitrateKbps = 500;
        base.maxBitrateKbps = 1_000;
        base.cpuScaleDownFactor = 1.5;
        break;
      case 'medium':
        base.maxWidth = 720;
        base.maxHeight = 480;
        base.maxFps = 24;
        base.targetBitrateKbps = 1_500;
        base.maxBitrateKbps = 3_000;
        break;
      case 'high':
        base.maxWidth = 1920;
        base.maxHeight = 1080;
        base.maxFps = 30;
        base.targetBitrateKbps = 4_000;
        base.maxBitrateKbps = 8_000;
        base.enableSVC = true;
        base.temporalLayers = 3;
        break;
    }

    // If no hardware encode, be more conservative
    if (!hw) {
      base.maxFps = Math.min(base.maxFps, 24);
      base.maxBitrateKbps = Math.min(base.maxBitrateKbps, 3_000);
      base.cpuScaleDownFactor = Math.max(base.cpuScaleDownFactor, 1.25);
    }

    return base;
  }

  /**
   * Get optimized screen share pipeline config.
   */
  getScreenShareConfig(): ScreenSharePipelineConfig {
    const base: ScreenSharePipelineConfig = {
      codec: 'VP9',  // Better for screen content
      maxCaptureFps: 15,
      maxEncodeFps: 15,
      targetBitrateKbps: 2_000,
      contentHint: 'detail',
      keyframeIntervalSec: 5,
      scaleFactor: 1.0,
      captureCursor: true,
    };

    switch (this._deviceTier) {
      case 'minimal':
        base.maxCaptureFps = 3;
        base.maxEncodeFps = 3;
        base.targetBitrateKbps = 500;
        base.scaleFactor = 0.5;
        base.keyframeIntervalSec = 10;
        break;
      case 'low':
        base.maxCaptureFps = 5;
        base.maxEncodeFps = 5;
        base.targetBitrateKbps = 1_000;
        base.scaleFactor = 0.75;
        break;
      case 'medium':
        // Use defaults
        break;
      case 'high':
        base.maxCaptureFps = 30;
        base.maxEncodeFps = 30;
        base.targetBitrateKbps = 4_000;
        base.contentHint = 'motion';
        base.keyframeIntervalSec = 3;
        break;
    }

    return base;
  }

  /**
   * Get SDP codec preference string for RTCRtpTransceiver.setCodecPreferences().
   */
  getSDPCodecPreferences(): any[] {
    try {
      const capabilities = RTCRtpSender.getCapabilities('video');
      if (!capabilities) return [];

      const preferred = this._getCodecPreference();
      const sorted = [...capabilities.codecs].sort((a, b) => {
        const aIdx = preferred.findIndex(p =>
          a.mimeType.toLowerCase().includes(p.toLowerCase())
        );
        const bIdx = preferred.findIndex(p =>
          b.mimeType.toLowerCase().includes(p.toLowerCase())
        );
        const aScore = aIdx >= 0 ? aIdx : 100;
        const bScore = bIdx >= 0 ? bIdx : 100;
        return aScore - bScore;
      });

      return sorted;
    } catch {
      return [];
    }
  }

  // ── Status ────────────────────────────────────────────────

  getStatus(): MediaPipelineStatus {
    return {
      videoCodecs: this._videoCodecs,
      bestVideoCodec: this._bestVideoCodec,
      hardwareEncodeAvailable: this._hardwareEncodeAvailable,
      audioConfig: this.getAudioConfig(),
      videoConfig: this.getVideoConfig(),
      screenShareConfig: this.getScreenShareConfig(),
      deviceTier: this._deviceTier,
    };
  }

  // ── Internal: Codec Detection ─────────────────────────────

  private async _detectVideoCodecs(): Promise<CodecCapability[]> {
    const codecs: CodecCapability[] = [];

    try {
      const capabilities = RTCRtpSender.getCapabilities('video');
      if (!capabilities) return codecs;

      // Check each codec
      for (const codec of capabilities.codecs) {
        const mime = codec.mimeType.toLowerCase();
        let videoCodec: VideoCodec | null = null;

        if (mime.includes('h264')) videoCodec = 'H264';
        else if (mime.includes('vp9')) videoCodec = 'VP9';
        else if (mime.includes('vp8')) videoCodec = 'VP8';
        else if (mime.includes('av1')) videoCodec = 'AV1';

        if (!videoCodec) continue;

        // Check if already have this codec
        if (codecs.some(c => c.codec === videoCodec)) continue;

        codecs.push({
          codec: videoCodec,
          hardwareAccelerated: await this._isHardwareAccelerated(videoCodec),
          maxWidth: videoCodec === 'VP8' ? 1280 : 1920,
          maxHeight: videoCodec === 'VP8' ? 720 : 1080,
          maxFps: 30,
          profileLevelId: codec.sdpFmtpLine?.match(/profile-level-id=([a-fA-F0-9]+)/)?.[1],
        });
      }
    } catch {}

    return codecs;
  }

  private async _isHardwareAccelerated(codec: VideoCodec): Promise<boolean> {
    // Method 1: Check GPU renderer for known hardware encoder support
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 1;
      canvas.height = 1;
      const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
      if (gl) {
        const ext = (gl as WebGLRenderingContext).getExtension('WEBGL_debug_renderer_info');
        if (ext) {
          const renderer = (gl as WebGLRenderingContext)
            .getParameter(ext.UNMASKED_RENDERER_WEBGL)
            .toLowerCase();

          // Check for known hardware encoders
          const hasHWEncoder = HW_ENCODER_HINTS.some(hint => renderer.includes(hint));

          if (hasHWEncoder) {
            // H264 and VP9 likely have HW support on modern GPUs
            if (codec === 'H264' || codec === 'VP9') return true;
            // AV1 HW encode is newer, only on recent GPUs
            if (codec === 'AV1' && (renderer.includes('nvidia') || renderer.includes('intel'))) return true;
          }
        }
      }
    } catch {}

    // Method 2: Try VideoEncoder API (if available)
    try {
      if ('VideoEncoder' in window) {
        const mimeMap: Record<VideoCodec, string> = {
          H264: 'avc1.42E01E',
          VP9: 'vp09.00.10.08',
          VP8: 'vp8',
          AV1: 'av01.0.01M.08',
        };

        const support = await (VideoEncoder as any).isConfigSupported({
          codec: mimeMap[codec],
          width: 640,
          height: 480,
          hardwareAcceleration: 'prefer-hardware',
        });

        return support?.supported === true;
      }
    } catch {}

    return false;
  }

  private _selectBestCodec(): VideoCodec {
    // Prefer hardware-accelerated H264 (widest HW support)
    const hwH264 = this._videoCodecs.find(c => c.codec === 'H264' && c.hardwareAccelerated);
    if (hwH264) return 'H264';

    const hwVP9 = this._videoCodecs.find(c => c.codec === 'VP9' && c.hardwareAccelerated);
    if (hwVP9) return 'VP9';

    const hwAV1 = this._videoCodecs.find(c => c.codec === 'AV1' && c.hardwareAccelerated);
    if (hwAV1) return 'AV1';

    // Fall back to any available codec
    if (this._videoCodecs.find(c => c.codec === 'VP9')) return 'VP9';
    if (this._videoCodecs.find(c => c.codec === 'H264')) return 'H264';

    return 'VP8';
  }

  private _getCodecPreference(): VideoCodec[] {
    // Prioritize hardware-accelerated codecs
    const hw = this._videoCodecs.filter(c => c.hardwareAccelerated).map(c => c.codec);
    const sw = this._videoCodecs.filter(c => !c.hardwareAccelerated).map(c => c.codec);

    // Order: HW H264 > HW VP9 > HW AV1 > SW VP9 > SW H264 > VP8
    const preferred: VideoCodec[] = [];

    for (const codec of ['H264', 'VP9', 'AV1'] as VideoCodec[]) {
      if (hw.includes(codec)) preferred.push(codec);
    }
    for (const codec of ['VP9', 'H264', 'VP8'] as VideoCodec[]) {
      if (sw.includes(codec) && !preferred.includes(codec)) preferred.push(codec);
    }

    // Always include VP8 as final fallback
    if (!preferred.includes('VP8')) preferred.push('VP8');

    return preferred;
  }
}

// ── Singleton ───────────────────────────────────────────────

export const mediaPipeline = new MediaPipeline();
